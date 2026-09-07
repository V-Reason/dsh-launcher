# -*- coding: utf-8 -*-
"""构建功能：检测仓库构建产物与源码时效，执行 pnpm run build（带动画）。

插件做不到的事：构建发生在 DSH 进程之外（仓库侧文件与 pnpm），
launcher 在启动前完成时效检测并按需构建。
"""

import os
import shutil

from .. import settings as settings_mod
from ..config import (
    BUILD_PROMPT,
    CLI_REL,
    DIST_REL,
    EXIT_BUILD,
    EXIT_USAGE,
    SRC_DIRS,
)
from ..console import die, step
from ..spinner import run_child_progress

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


def run_build(repo):
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        die("未找到 pnpm（请安装 pnpm 并加入 PATH）", EXIT_BUILD)
    code = run_child_progress([pnpm, "run", "build"], "构建中…", cwd=repo)
    if code != 0:
        die("构建失败（exit code %d），请手动检查" % code, EXIT_BUILD)
    step("构建完成。")


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
