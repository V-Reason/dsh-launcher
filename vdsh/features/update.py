# -*- coding: utf-8 -*-
"""更新功能：vdsh update dsh | vdsh update plugin（Harness 本体 / profile 插件分开更新）。

插件做不到的事：更新发生在 DSH 进程之外——Harness 检出的 git pull、pnpm install
与构建，以及 profile 目录的插件依赖更新——故在 launcher 层实现。

- `vdsh update dsh`：Harness 检出 git fetch + merge（@upstream）+ pnpm install +
  pnpm run build（与 README 的从源安装流程一致），并显示新旧版本号。
- `vdsh update plugin [profile]`：经官方 dsh CLI 转发
  `pnpm update --latest`（自带 bundle 层重调解——新版本声明了 dsh.bundle 会自动
  激活，直连 pnpm 拿不到这一行为）；默认 profile = web。结束后按 profile_state 的
  三重证据校验「确实更新了 / 确实是最新」：manifest 声明 ↔ pnpm-lock.yaml 记录 ↔
  磁盘 .modules.yaml 解析身份（git 依赖比 commit，不看版本号），并标明生效方式；
  校验不通过即硬失败。
- 两者都要求 dsh web 未运行（Windows 文件锁 + 更新后需重启才生效）：运行中会询问，
  非交互/EOF 默认中止。
- TTY 下折叠 pnpm 的低价值行（进度/重试/统计），进度改写进转轮文案；失败时补打被
  折叠行（机制见 spinner.run_child_progress，规则见 `_PnpmNoise`）。
"""

import json
import os
import re
import shutil
import subprocess

from .. import profile_state
from .. import settings as settings_mod
from ..config import (
    CLI_REL,
    EXIT_BUILD,
    EXIT_DEPS,
    EXIT_ERROR,
    EXIT_USAGE,
)
from ..console import ask, die, fail, step, warn
from ..spinner import run_child_progress

NAME = "update"
SUMMARY = "更新 DSH 或插件（update dsh | update plugin）"

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# pnpm 低价值输出（TTY 折叠；非 TTY 全量透传）：(规则, 种类)
PNPM_QUIET_RULES = (
    (re.compile(r"^Progress:\s+resolved\s+(\d+),\s+reused\s+(\d+)"), "progress"),
    (re.compile(r"^\[WARN\]\s+(GET|HEAD|POST)\s+\S+\s+error\s+\(.+?\)\..*retries?\s+left\."), "retry"),
    (re.compile(r"^\[WARN\]\s+Issues with peer dependencies found"), "peers"),
    (re.compile(r"^Packages:\s*[-+]\d"), "stats"),
    (re.compile(r"^[-+]{2,}$"), "bar"),
    (re.compile(r"^Already up to date$"), "noop"),
    (re.compile(r"^Done in \S+ using pnpm"), "done"),
    (re.compile(r"^✓\s+Lockfile\b.*passes supply-chain policies"), "supply"),
)

# pnpm 网络类硬失败指纹：命中则按网络原因提示（退出码语义不变）。
PNPM_NETFAIL_RE = re.compile(
    r"ERR_PNPM_[A-Z_]*(?:FETCH|NETWORK|TIMEOUT)[A-Z_]*"
    r"|Seems like you have internet connection issues"
)


class _PnpmNoise:
    """折叠 pnpm 低价值行：统计计数 + 生成转轮文案 + 结果行尾注。

    作为 `quiet` 回调传给 run_child_progress：返回 None = 该行照常打印；
    返回字符串 = 折叠该行（仅 TTY），非空字符串同时作为新的转轮文案。
    """

    def __init__(self):
        self.resolved = 0
        self.reused = 0
        self.retries = 0
        self.peers = False
        self.supply_chain = False
        self.observed = False  # 是否看到过 pnpm 的运行标记（证明 pnpm 真的跑了）

    def __call__(self, line):
        clean = ANSI_RE.sub("", line).strip()
        for pattern, kind in PNPM_QUIET_RULES:
            match = pattern.match(clean)
            if match is None:
                continue
            self.observed = True
            if kind == "progress":
                self.resolved = int(match.group(1))
                self.reused = int(match.group(2))
                return self._progress_hint()
            if kind == "retry":
                self.retries += 1
                return self._progress_hint()
            if kind == "peers":
                self.peers = True
                return ""  # 折叠（空串 = 不改转轮文案，进度行信息量更大）
            if kind == "supply":
                self.supply_chain = True
            return ""
        return None

    def _progress_hint(self):
        parts = ["已解析 %d" % self.resolved]
        if self.reused:
            parts.append("复用 %d" % self.reused)
        if self.retries:
            parts.append("网络重试 %d" % self.retries)
        return " · ".join(parts)

    def hint(self):
        """结果行尾注：只列实际发生的项，无命中则空串（不额外加噪）。"""
        parts = []
        if not self.observed:
            # dsh plugin 退出码 0 却没有任何 pnpm 运行标记：更新可能根本没执行，
            # 此时「已确认为最新」只说明状态没变，必须说清楚（不判失败）。
            parts.append("未见 pnpm 运行标记（更新可能未真正执行，可用 vdsh doctor 复核）")
        if self.retries:
            parts.append("网络重试 %d 次（GitHub 探测失败→回退 git 解析，非错误）" % self.retries)
        if self.peers:
            parts.append("缺 peer 提示（profile 设计预期；pnpm peers check 看明细）")
        return "；".join(parts)


def _has_network_failure(lines):
    """子进程输出里是否命中网络类失败指纹。"""
    for line in lines or ():
        if PNPM_NETFAIL_RE.search(ANSI_RE.sub("", line)):
            return True
    return False


def _usage():
    print("用法: vdsh update <dsh | plugin> […]")
    print("  vdsh update dsh                更新 Harness 本体（git pull + pnpm install + build）")
    print("  vdsh update plugin [profile]   更新 profile 插件依赖（默认 profile = web）")


def _git_quiet(repo, *args):
    """在 repo 静默执行 git（本地只读/自查操作），返回 (退出码, 输出文本)。"""
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1, ""
    text = (proc.stdout or "").strip()
    if proc.stderr and proc.stderr.strip():
        text = text + ("\n" if text else "") + proc.stderr.strip()
    return proc.returncode, text


def _version_of(package_json_dir):
    """读取 package.json 的 version；异常返回 '?'。"""
    try:
        with open(os.path.join(package_json_dir, "package.json"), "r", encoding="utf-8") as handle:
            return json.load(handle).get("version", "?")
    except (OSError, ValueError):
        return "?"


def _confirm_still_updated():
    """dsh web 运行中时的中止询问：返回 True = 继续，False = 用户取消。"""
    from . import launch as launch_mod  # 延迟导入（probe_harness）

    status = launch_mod.probe_harness()
    if status == "idle":
        return True
    warn("检测到 dsh web 正在运行（%s）；更新前请先停止服务——Windows 下文件可能被锁定，"
         "且新版需重启才生效。" % status)
    answer = ask("仍要继续更新？[y/N] ", default="n")
    return (answer or "n").strip().lower() in ("y", "yes")


def _update_dsh(settings):
    """更新 Harness 本体：fetch → merge @upstream → pnpm install → build。"""
    repo = settings_mod.effective_repo(settings)
    if not os.path.isfile(os.path.join(repo, "package.json")):
        die("未找到 Harness 仓库（%s），请设置 DSH_REPO 或编辑 vdsh.yaml 的 launcher.repo" % repo,
            EXIT_USAGE)

    if not _confirm_still_updated():
        step("已取消（停止 dsh web 后重试）")
        return 0

    code, _ = _git_quiet(repo, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        die("%s 不是 git 检出，无法自动更新；请用 git clone 安装（见 Harness README）后重试。" % repo,
            EXIT_USAGE)

    code, dirty = _git_quiet(repo, "status", "--porcelain")
    if code == 0 and dirty:
        lines = dirty.splitlines()
        warn("仓库有 %d 个未提交改动（如 %s …）；更新可能冲突或覆盖本地改动。"
             % (len(lines), lines[0].strip()))
        answer = ask("仍要继续更新？[y/N] ", default="n")
        if (answer or "n").strip().lower() not in ("y", "yes"):
            step("已取消（处理完本地改动后重试，如 git stash）")
            return 0

    old_version = _version_of(repo)
    code = run_child_progress(["git", "fetch", "origin"], "获取远端更新…", cwd=repo)
    if code != 0:
        die("获取远端更新失败（git fetch origin）：请确认网络与远端可达。", EXIT_ERROR)

    code, upstream = _git_quiet(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
    if code != 0 or not upstream:
        die("仓库未配置上游分支（@{upstream}），无法自动更新；请先 git push -u 建立跟踪后重试。",
            EXIT_USAGE)
    upstream = upstream.splitlines()[0].strip()

    ahead, behind = 0, 0
    code, counts = _git_quiet(repo, "rev-list", "--left-right", "--count",
                              "HEAD...%s" % upstream)
    if code == 0 and counts:
        match = re.match(r"^(\d+)\s+(\d+)$", counts.strip())
        if match:
            ahead, behind = int(match.group(1)), int(match.group(2))
    if behind == 0:
        step("dsh 已是最新（%s）%s" % (old_version,
                                       ("；本地领先 %d 提交（可 git push）" % ahead) if ahead else ""))
        return 0

    step("远端更新: %d 个提交" % behind)
    if ahead > 0:
        step("仓库本地另有 %d 个提交，将常规合并；冲突需手动处理。" % ahead)
        code = run_child_progress(
            ["git", "merge", "-m", "vdsh: sync 远端更新", upstream],
            "合并远端更新…", cwd=repo)
    else:
        code = run_child_progress(
            ["git", "merge", "--ff-only", upstream],
            "合并远端更新…", cwd=repo)
    if code != 0:
        die("合并远端更新失败：请检查冲突（git status）后重试，或手动 git pull。", EXIT_ERROR)

    new_version = _version_of(repo)
    step("版本: %s → %s" % (old_version,
                            new_version if new_version != old_version else "（未变）"))

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        die("未找到 pnpm（请安装 pnpm 并加入 PATH）", EXIT_DEPS)
    noise = _PnpmNoise()
    lines = []
    code = run_child_progress([pnpm, "install"], "安装依赖…", cwd=repo,
                              collect=lines, quiet=noise)
    if code != 0:
        tail = "；疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试，见 doc/usage.md）" \
            if _has_network_failure(lines) else "；请手动重试。"
        die("pnpm install 失败（exit code %d）%s" % (code, tail), EXIT_ERROR)
    if noise.observed and noise.hint():
        step("pnpm：%s" % noise.hint())

    from . import build as build_feature
    build_feature.run_build(repo)  # 失败时内部 die(EXIT_BUILD)

    step("dsh 已更新完成（%s）" % new_version)
    warn("dsh web 需重启后生效（停止旧实例后重新 vdsh 启动）")
    return 0


def _plugin_result_line(updated, added, removed, unchanged, after, noise):
    """一行结论：更新了什么 + 每个插件的生效方式 + pnpm 噪声尾注。"""
    total = len(after["plugins"])
    if updated:
        shown = updated[:3]
        detail = "、".join("%s %s→%s" % (name, old, new) for name, old, new in shown)
        if len(updated) > len(shown):
            detail += " 等 %d 个" % len(updated)
        head = "插件已更新 %d 个：%s" % (len(updated), detail)
        if unchanged:
            head += "（其余 %d 个未变）" % len(unchanged)
    elif added or removed:
        bits = []
        if added:
            bits.append("新增 %s" % "、".join(added))
        if removed:
            bits.append("移除 %s" % "、".join(removed))
        head = "插件清单已变化：%s" % "；".join(bits)
    else:
        head = "插件已确认为最新：%d 个依赖的解析与 lockfile、磁盘三方一致" % total
    line = "%s；生效方式：%s" % (head, profile_state.activation_summary(after))
    hint = noise.hint()
    if hint:
        line += "｜pnpm：" + hint
    return line


def _update_plugin(settings, profile):
    """更新 profile 插件依赖：dsh plugin 转发 pnpm update --latest，再校验结果。"""
    if not PROFILE_RE.match(profile):
        die("profile 名不合法: %s（仅小写字母/数字/连字符）" % profile, EXIT_USAGE)

    repo = settings_mod.effective_repo(settings)
    data_dir = settings_mod.effective_data_dir(settings)
    if not data_dir:
        data_dir = os.path.join(os.path.expanduser("~"), ".dsh")
    profile_dir = os.path.join(data_dir, "profiles", profile)
    if not os.path.isfile(os.path.join(profile_dir, "package.json")):
        die("profile 未初始化: %s（首次运行 dsh 后生成；或用 dsh plugin --profile %s add <包> 安装插件）"
            % (profile_dir, profile), EXIT_USAGE)

    if not _confirm_still_updated():
        step("已取消（停止 dsh web 后重试）")
        return 0

    before = profile_state.verify(profile_dir, data_dir=data_dir, repo=repo)
    if not before["plugins"]:
        step("无插件依赖（profile 仅平台默认 bundle）")
        return 0

    node = shutil.which("node")
    if node is None:
        die("未找到 node：请安装 Node.js 并加入 PATH", EXIT_DEPS)
    cli = os.path.join(repo, CLI_REL)
    if not os.path.isfile(cli):
        die("未找到 dsh CLI 产物（%s），请先执行 vdsh build" % cli, EXIT_BUILD)

    step("更新 %d 个插件依赖（%s）…" % (len(before["plugins"]), profile_dir))
    noise = _PnpmNoise()
    lines = []
    code = run_child_progress(
        [node, cli, "plugin", "--profile", profile, "update", "--latest"],
        "更新插件…", collect=lines, quiet=noise)
    if code == 127:
        die("未找到 pnpm（dsh plugin 报错）：请安装 pnpm 并加入 PATH", EXIT_DEPS)
    if code != 0:
        tail = "；疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试，见 doc/usage.md）" \
            if _has_network_failure(lines) else "；查看上方 pnpm 输出"
        die("插件更新失败（exit code %d）%s" % (code, tail), EXIT_ERROR)

    after = profile_state.verify(profile_dir, data_dir=data_dir, repo=repo)
    updated, added, removed, unchanged = profile_state.diff_plugins(before, after)
    step(_plugin_result_line(updated, added, removed, unchanged, after, noise))

    for text in after["warnings"]:
        warn(text)
    if after["failures"]:
        for text in after["failures"]:
            fail(text)
        die("插件更新后校验未通过（见上）：请处理后再重启 dsh web", EXIT_ERROR)

    warn("重启 dsh web 后生效；记得 vdsh sync push 让副机同步（profiles/%s 在同步范围内）" % profile)
    return 0


def run(argv, settings):
    """功能入口：vdsh update <dsh | plugin>。"""
    if not argv:
        _usage()
        return EXIT_USAGE
    sub = argv[0]
    if sub == "dsh":
        if len(argv) > 1:
            die("update dsh 不接受额外参数", EXIT_USAGE)
        return _update_dsh(settings)
    if sub == "plugin":
        if len(argv) > 2:
            die("update plugin 最多接受 1 个 profile 名", EXIT_USAGE)
        return _update_plugin(settings, argv[1] if len(argv) > 1 else "web")
    die("未知 update 子命令: %s（用法: vdsh update <dsh | plugin>）" % sub, EXIT_USAGE)
