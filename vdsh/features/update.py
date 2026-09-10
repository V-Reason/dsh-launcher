# -*- coding: utf-8 -*-
"""更新功能：vdsh update dsh | vdsh update plugin（Harness 本体 / profile 插件分开更新）。

插件做不到的事：更新发生在 DSH 进程之外——Harness 检出的 git pull、pnpm install
与构建，以及 profile 目录的插件依赖更新——故在 launcher 层实现。

- `vdsh update dsh`：Harness 检出 git fetch + merge（@upstream）+ pnpm install +
  pnpm run build（与 README 的从源安装流程一致），并显示新旧版本号。
  失败诊断不受输出简约约束：merge/install 失败给 exit code 与恢复路径，build 失败
  由 `features/build.run_build` 补打末尾输出并落盘完整日志（改前只有一句「请手动检查」，
  真失败与误报在终端上无法区分）。
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
import time

from .. import profile_state
from .. import settings as settings_mod
from ..config import (
    CLI_REL,
    EXIT_BUILD,
    EXIT_DEPS,
    EXIT_ERROR,
    EXIT_USAGE,
)
from ..console import ask, die, fail, ok, progress, step, warn
from ..pnpm_log import Noise, has_network_failure
from ..spinner import run_child_progress

NAME = "update"
SUMMARY = "更新 DSH 或插件（update dsh | update plugin）"

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _usage():
    print("用法: vdsh update <dsh | plugin> […]")
    print("  vdsh update dsh                更新 Harness 本体（git pull + pnpm install + build）")
    print("  vdsh update plugin [profile]   更新 profile 插件依赖（默认 profile = web）")


def _git_quiet(repo, *args):
    """在 repo 静默执行 git（本地只读/自查操作），返回 (退出码, 输出文本)。

    输出文本 = stdout + stderr 合并：调用方若要做「是否有内容」的判定（脏工作区、
    分支名解析），必须改用 `_git_stdout`——git 的警告（换行符、SSH known_hosts 等）
    走 stderr，合并后会变成「干净仓库凭空有改动」的误判。
    """
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


def _git_stdout(repo, *args):
    """同 `_git_quiet`，但**只取 stdout** 并逐行返回（供「有/无内容」类判定）。

    返回 (退出码, [非空行…])；与 `_git_quiet` 一样吞掉可控异常并给 -1。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1, []
    lines = [line.strip() for line in (proc.stdout or "").splitlines()]
    return proc.returncode, [line for line in lines if line]


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
    warn("dsh web 正在运行（%s）：更新前请先停止服务（文件可能被锁定，且新版需重启才生效）"
         % status)
    answer = ask("仍要继续更新？[y/N] ", default="n")
    return (answer or "n").strip().lower() in ("y", "yes")


def _valid_upstream(name):
    """上游分支名形状校验：只接受「<remote>/<branch>」两段且无空白/控制字符。

    刻意**不限制为非 ASCII**：git 分支名允许中文（`git rev-parse --abbrev-ref @{upstream}`
    会原样返回 `origin/中文分支`），用 ASCII 白名单会把合法仓库判成「未配置上游」。
    目的是挡住解析异常/警告文本被当成 ref 喂给后续 git 命令。
    """
    if not name or any(char.isspace() or ord(char) < 0x20 for char in name):
        return False
    parts = name.split("/")
    return len(parts) == 2 and all(parts)


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

    code, dirty = _git_stdout(repo, "status", "--porcelain")
    if code == 0 and dirty:
        warn("仓库有 %d 个未提交改动（如 %s …）：合并可能失败或覆盖本地改动"
             % (len(dirty), dirty[0]))
        answer = ask("仍要继续更新？[y/N] ", default="n")
        if (answer or "n").strip().lower() not in ("y", "yes"):
            step("已取消（处理完本地改动后重试，如 git stash；自查 git status）")
            return 0

    old_version = _version_of(repo)
    started = time.monotonic()
    code = run_child_progress(["git", "fetch", "origin"], "获取远端更新", cwd=repo)
    if code != 0:
        die("获取远端更新失败（git fetch origin）：请确认网络与远端可达。", EXIT_ERROR)

    code, upstream_lines = _git_stdout(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
    upstream = upstream_lines[0] if upstream_lines else ""
    # 形状校验：@{upstream} 解析失败或输出异常时，不要把杂串喂给后续 git 命令。
    if code != 0 or not _valid_upstream(upstream):
        die("仓库未配置上游分支（@{upstream}），无法自动更新；请先 git push -u 建立跟踪后重试。",
            EXIT_USAGE)

    ahead, behind = 0, 0
    code, counts = _git_stdout(repo, "rev-list", "--left-right", "--count",
                               "HEAD...%s" % upstream)
    if code == 0 and counts:
        match = re.match(r"^(\d+)\s+(\d+)$", counts[0])
        if match:
            ahead, behind = int(match.group(1)), int(match.group(2))
    if behind == 0:
        ok("dsh 已是最新（%s）" % old_version)
        if ahead:
            step("本地领先 %d 个提交（可 git push）" % ahead)
        step("用时 %s" % _format_duration(time.monotonic() - started))
        return 0

    progress("远端更新 %d 个提交" % behind)
    if ahead > 0:
        progress("本地另有 %d 个提交（常规合并，冲突需手动处理）" % ahead)
        code = run_child_progress(
            ["git", "merge", "-m", "vdsh: sync 远端更新", upstream],
            "合并远端更新", cwd=repo)
    else:
        code = run_child_progress(
            ["git", "merge", "--ff-only", upstream],
            "合并远端更新", cwd=repo)
    if code != 0:
        if ahead == 0:
            # 本地没有领先提交却快进失败：不是冲突，而是上游历史被改写（force push/重建分支）。
            die("合并远端更新失败（无法快进，本地无领先提交）：上游可能已被 force push 改写。"
                "请先确认 `git log --oneline HEAD..%s`，再决定合并还是 `git reset --hard %s`；"
                "手动处理完重跑本条命令即可。" % (upstream, upstream), EXIT_ERROR)
        die("合并远端更新失败：请检查冲突（git status）后重试，或手动 git pull。", EXIT_ERROR)

    new_version = _version_of(repo)
    progress("版本 %s → %s" % (old_version,
                               new_version if new_version != old_version else "（未变）"))

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        die("未找到 pnpm（请安装 pnpm 并加入 PATH）", EXIT_DEPS)
    noise = Noise()
    lines = []
    code = run_child_progress([pnpm, "install"], "安装依赖", cwd=repo,
                              collect=lines, quiet=noise)
    if code != 0:
        tail = "，疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试）" \
            if has_network_failure(lines) else "，请手动重试"
        die("pnpm install 失败（exit code %d%s）" % (code, tail), EXIT_ERROR)

    from . import build as build_feature
    # 失败时内部 die(EXIT_BUILD)；code_is_new=True → 诊断里点明「代码已更新、仅构建未完成」。
    build_feature.run_build(repo, code_is_new=True)

    ok("dsh 已更新（%s）" % new_version)
    step("用时 %s" % _format_duration(time.monotonic() - started))
    warn("重启 dsh web 后生效")
    return 0


def _format_duration(seconds):
    """人类可读耗时：46.2s / 1m46s / 1h02m。"""
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


def _plugin_result_line(updated, added, removed, unchanged, after):
    """一句结论：更新了什么 / 是否已是最新（耗时由调用方单独一行打印）。"""
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
        head = "插件已是最新（%d 个依赖校验通过）" % len(after["plugins"])
    return head


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

    step("更新 %d 个插件依赖（profiles/%s）…" % (len(before["plugins"]), profile))
    noise = Noise()
    lines = []
    started = time.monotonic()
    code = run_child_progress(
        [node, cli, "plugin", "--profile", profile, "update", "--latest"],
        "更新插件", collect=lines, quiet=noise)
    duration = _format_duration(time.monotonic() - started)
    if code == 127:
        die("未找到 pnpm（dsh plugin 报错）：请安装 pnpm 并加入 PATH", EXIT_DEPS)
    if code != 0:
        tail = "，疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试）" \
            if has_network_failure(lines) else "，见上方 pnpm 输出"
        die("插件更新失败（exit code %d%s）" % (code, tail), EXIT_ERROR)

    after = profile_state.verify(profile_dir, data_dir=data_dir, repo=repo)
    updated, added, removed, unchanged = profile_state.diff_plugins(before, after)
    ok(_plugin_result_line(updated, added, removed, unchanged, after))
    step("用时 %s" % duration)
    if not noise.observed:
        # dsh plugin 退出码 0 却没有任何 pnpm 运行标记：更新可能根本没执行，
        # 此时结论只说明状态没变（不判失败，但必须说清楚）。
        warn("未见 pnpm 运行标记：本次可能未真正执行更新，可用 vdsh doctor 复核")

    for text in after["warnings"]:
        warn(text)
    if after["failures"]:
        for text in after["failures"]:
            fail(text)
        die("插件更新后校验未通过（见上）：请处理后再重启 dsh web", EXIT_ERROR)

    warn("重启 dsh web 后生效")
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
