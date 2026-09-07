# -*- coding: utf-8 -*-
"""同步功能：DSH 数据同步（vdsh sync / vdsh --sync 的直通/动画分流）。

插件做不到的事：DSH 数据（聊天记录、设置、插件数据）的备份与双机同步，
必须发生在 DSH 进程之外（原生 Git）——插件方案会让同步命令自身写入
sessions/，产生无法收敛的自指残差（先前的 dsh-data-sync 插件即因此作废）。
因此本功能在 launcher 层直通独立的 sync-dsh.ps1 子工具。

- push/pull/init：长时等待（git fetch/push/merge），捕获输出流式显示 + 动画；
- 无参（交互菜单，内部 Read-Host）、remote / status / help（本地快）：继承 stdio 直通。
  退出码语义透传（0 成功 / 1 硬失败 / 2 用法错误 / 3 被阻塞 / 4 未初始化）。

配置经「环境桥」注入 sync-dsh.ps1（VDG_SYNC_* / VDG_ANIMATION_* / DSH_HOME）：
仅通过 vdsh 调用时生效；直接/菜单调用脚本时无这些变量 → 完全回退脚本内置默认。
"""

import json
import os
import subprocess

from .. import settings as settings_mod
from ..config import (
    EXIT_USAGE,
    SYNC_BLOCKED,
    SYNC_NOT_SETUP,
    SYNC_OK,
    SYNC_PS1,
    sync_host,
)
from ..console import die
from ..spinner import run_child_progress

NAME = "sync"
SUMMARY = "DSH 数据同步（init/push/pull/status/remote；无参 = 交互菜单）"

# 需要动画的子命令（长时等待）；status/remote 为本地操作、help 即时，直通即可。
ANIMATED_SUBCOMMANDS = ("push", "pull", "init")
MESSAGES = {
    "push": "推送 DSH 数据…",
    "pull": "拉取 DSH 数据…",
    "init": "初始化 DSH 数据同步…",
}


def _command(sync_args):
    return [sync_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SYNC_PS1)] + list(sync_args)


def _child_env(settings):
    """构建子进程环境：把 vdsh.yaml 的 sync/animation 配置以 VDG_* 变量写入。

    规则：DSH_HOME 取 effective_data_dir（env 优先、其次 sync.data_dir），
    并经 ~ 展开为绝对路径后注入（配置与 env 均支持 ~/.dsh 写法）；
    VDG_SYNC_* 始终以配置值覆盖（脚本缺省时无此变量 = 内置默认）。
    """
    env = os.environ.copy()
    data_dir = settings_mod.effective_data_dir(settings)
    if data_dir:
        env["DSH_HOME"] = data_dir

    allowlist = settings["sync"]["allowlist"]
    if allowlist:
        env["VDG_SYNC_ALLOWLIST"] = json.dumps(allowlist, ensure_ascii=False)
    if settings["sync"]["gitignore_extra"]:
        env["VDG_SYNC_GITIGNORE_EXTRA"] = settings["sync"]["gitignore_extra"]
    env["VDG_SYNC_COMMIT_NAME"] = settings["sync"]["commit_name"]
    env["VDG_SYNC_COMMIT_EMAIL"] = settings["sync"]["commit_email"]
    if settings["sync"]["timeout_seconds"] > 0:
        env["VDG_SYNC_TIMEOUT"] = str(settings["sync"]["timeout_seconds"])
    env["VDG_ANIMATION_FPS"] = str(settings["animation"]["fps"])
    env["VDG_ANIMATION_FRAMES"] = settings["animation"]["frames"]
    return env


def run_sync(sync_args, settings):
    """在当前控制台运行同步脚本，返回其退出码。

    脚本位置唯一：本目录 dsh-data-git-sync/sync-dsh.ps1；缺失时按「未初始化」(4) 处理。
    init 无 URL 时回退 vdsh.yaml 的 sync.remote。
    """
    if not SYNC_PS1.is_file():
        print("vdsh ⚠ 同步脚本缺失: %s" % SYNC_PS1, file=sys.stderr)
        return SYNC_NOT_SETUP

    sub = sync_args[0].lower() if sync_args else ""
    if sub == "init":
        url = sync_args[1] if len(sync_args) > 1 else settings["sync"]["remote"]
        if not url:
            print("vdsh ✗ 缺少远程地址。用法: vdsh sync init <远程URL>"
                  "（或在 vdsh.yaml 配置 sync.remote）", file=sys.stderr)
            return 2
        sync_args = ["init", url]

    env = _child_env(settings)
    command = _command(sync_args)
    timeout = settings["sync"]["timeout_seconds"] or None
    if sub in ANIMATED_SUBCOMMANDS:
        try:
            return run_child_progress(command, MESSAGES[sub], env=env, timeout=timeout)
        except OSError as error:
            print("vdsh ⚠ 无法启动同步脚本（%s）" % error, file=sys.stderr)
            return 1
    try:
        return subprocess.call(command, env=env)
    except OSError as error:
        print("vdsh ⚠ 无法启动同步脚本（%s）" % error, file=sys.stderr)
        return 1


def auto_sync_pull(settings):
    """启动服务前的自动数据拉取（--sync 开启）。

    任何结果都不阻塞启动：0 成功；4 未初始化（静默提示一句）；3 被阻塞 / 其它失败告警。
    """
    code = run_sync(["pull"], settings)
    if code == SYNC_OK:
        print("vdsh · 数据已同步。")
    elif code == SYNC_NOT_SETUP:
        print("vdsh · 未检测到数据同步仓库，跳过（需先执行 vdsh sync init <远程URL>）")
    elif code == SYNC_BLOCKED:
        print("vdsh ⚠ 数据同步被阻塞（见上方提示）；继续启动。")
    else:
        print("vdsh ⚠ 数据同步失败（退出码 %d）；继续启动。" % code)


def run(argv, settings):
    """功能入口：vdsh sync [子命令...] → 直通 sync-dsh.ps1，退出码原样透传。"""
    for arg in argv:
        if arg.startswith("-"):
            die("sync 子命令不接受启动器选项（%s）" % arg, EXIT_USAGE)
    return run_sync(argv, settings)
