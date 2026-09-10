# -*- coding: utf-8 -*-
"""构建功能：检测仓库构建产物与源码时效，执行 pnpm run build（带动画）。

插件做不到的事：构建发生在 DSH 进程之外（仓库侧文件与 pnpm），
launcher 在启动前完成时效检测并按需构建。

输出遵循同一标准：转轮 = 任务性质 + 秒数，pnpm 自身噪声折叠成 `→ …` 进度行
（构建工具的真实输出照常透传），结束 `vdsh ✓ 构建完成` + 单独一行耗时。

失败诊断（不受输出简约约束）：子进程输出全程落盘到 `config.BUILD_LOG_PATH`，
非 0 退出时复述**末尾 15 行**（构建工具的真报错都在尾部）+ 完整日志路径，
并说明「代码已更新、仅构建失败」，避免只有一句 exit code 的「无证据失败」。
"""

import os
import shutil
import sys
import time

from .. import settings as settings_mod
from ..config import (
    BUILD_LOG_PATH,
    BUILD_PROMPT,
    BUILD_TAIL_LINES,
    CLI_REL,
    DIST_REL,
    EXIT_BUILD,
    EXIT_DEPS,
    EXIT_USAGE,
    SRC_DIRS,
)
from ..console import die, ok, step, warn
from ..pnpm_log import Noise, has_network_failure
from ..spinner import EXIT_SPAWN_FAILED, run_child_progress

NAME = "build"
SUMMARY = "执行仓库构建（pnpm run build，带动画）"


def newest_mtime(root):
    """目录内所有文件的最大 mtime（Windows 上目录自身 mtime 不随内容编辑变化）。"""
    newest = 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    return newest


def build_needed(repo):
    cli_bin = os.path.join(repo, CLI_REL)
    dist = os.path.join(repo, DIST_REL)
    if not os.path.isfile(cli_bin) or not os.path.isfile(dist):
        return True
    stale = False
    for rel in SRC_DIRS:
        src = os.path.join(repo, rel)
        if not os.path.isdir(src):
            continue
        target = cli_bin if rel.startswith(os.path.join("apps", "cli")) else dist
        if os.path.isfile(target) and newest_mtime(src) > os.path.getmtime(target):
            stale = True
    return stale


def confirm_build():
    try:
        answer = input(BUILD_PROMPT).strip().lower()
    except EOFError:
        # 非交互环境（stdin 重定向）：默认不构建、直接退出。
        return False
    return answer in ("", "y", "yes")


def _write_build_log(lines):
    """把构建输出全文落盘（失败时可回溯）；写不进去只告警，不影响构建结论。"""
    try:
        with open(BUILD_LOG_PATH, "w", encoding="utf-8", errors="replace") as handle:
            for line in lines:
                handle.write(line + "\n")
        return True
    except OSError as error:
        warn("构建日志未能写入 %s：%s" % (BUILD_LOG_PATH, error))
        return False


def _prune_build_log():
    """构建成功后删除日志：只保留最近一次失败现场，避免 %TEMP% 堆积。"""
    try:
        os.remove(BUILD_LOG_PATH)
    except OSError:
        pass


def run_build(repo, pnpm=None, code_is_new=False):
    """执行 pnpm run build；失败时给出 exit code + 末尾输出 + 完整日志路径。

    pnpm：可选的 pnpm 可执行文件路径（默认按 PATH 解析；测试用注入 stub）。
    code_is_new：调用方是否刚把检出的代码更新到最新（`vdsh update dsh` 传 True）——
    只影响失败收尾那句「代码已更新、仅构建失败」是否成立。
    """
    if pnpm is None:
        pnpm = shutil.which("pnpm")
    if pnpm is None:
        # 退出码与 update 侧一致：pnpm 缺失是环境依赖问题（EXIT_DEPS），不是构建失败。
        die("未找到 pnpm（请安装 pnpm 并加入 PATH）", EXIT_DEPS)
    noise = Noise()
    lines = []
    started = time.monotonic()
    # 失败时**只看末尾几行**（构建工具的真报错都在尾部、且一定没被折叠）：
    # 用 collect 收全量、自己只打末尾，避免「折叠行补打 + tail 复述」把同一批行打两遍。
    code = run_child_progress([pnpm, "run", "build"], "构建", cwd=repo, quiet=noise,
                              collect=lines)
    duration = time.monotonic() - started
    if code == EXIT_SPAWN_FAILED:
        die("无法启动 pnpm（%s）：请确认可执行且未被安全软件拦截" % pnpm, EXIT_DEPS)
    if code != 0:
        _report_build_failure(repo, code, lines, code_is_new)
        die("构建失败（exit code %d）" % code, EXIT_BUILD)
    _prune_build_log()
    ok("构建完成")
    step("用时 %.1fs" % duration)


def _report_build_failure(repo, code, lines, code_is_new):
    """失败诊断（不受输出简约约束）：末尾输出 + 完整日志 + 仓库状态说明。

    code_is_new：调用方是否刚把检出的代码更新到最新（update dsh 的构建段）——
    决定收尾那句是「代码已更新、仅构建失败」还是纯「构建失败」。
    """
    print("", file=sys.stderr)
    warn("构建输出末尾 %d 行（完整日志见下）：" % min(BUILD_TAIL_LINES, len(lines)))
    for line in lines[-BUILD_TAIL_LINES:]:
        print(line, file=sys.stderr)
    if _write_build_log(lines):
        warn("完整构建日志：%s" % BUILD_LOG_PATH)
    if has_network_failure(lines):
        warn("构建失败（exit code %d），疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试）" % code)
    else:
        warn("构建失败（exit code %d）：上面末尾输出即根因线索" % code)
    if code_is_new:
        warn("仓库代码已更新到最新，仅构建未完成；修复后可重跑 `vdsh build`，"
             "或在 %s 手动执行 `pnpm install && pnpm run build`" % repo)
    else:
        warn("可修复后重跑 `vdsh build`，或在 %s 手动执行 `pnpm install && pnpm run build`" % repo)


def run(argv, settings):
    """独立命令：vdsh build（直接构建，不经确认）。"""
    if argv:
        die("build 不接受参数", EXIT_USAGE)
    repo = settings_mod.effective_repo(settings)
    if not os.path.isfile(os.path.join(repo, "package.json")):
        die("未找到 Harness 仓库（%s），请设置 DSH_REPO 或编辑 vdsh.yaml 的 launcher.repo" % repo,
            EXIT_USAGE)
    run_build(repo)
    return 0
