# -*- coding: utf-8 -*-
"""同步功能：DSH 数据同步（vdsh sync / vdsh --sync 的直通/动画分流）。

插件做不到的事：DSH 数据（聊天记录、设置、插件数据）的备份与双机同步，
必须发生在 DSH 进程之外（原生 Git）——插件方案会让同步命令自身写入
sessions/，产生无法收敛的自指残差（先前的 dsh-data-sync 插件即因此作废）。
因此本功能在 launcher 层直通独立的 sync-dsh.ps1 子工具。

- push/pull/init：长时等待（git fetch/push/merge），捕获输出流式显示 + 动画；
  pull 由脚本打印阶段行与内容摘要（远端新增 N 提交 · M 文件），与 push 反映
  推送内容对称；脚本步骤行在「直接终端」与「经 vdsh」两种模式下都可见
  （PS 层动画的 -Message 仅在直接终端可见，不能作为唯一进度来源）；
- init 无 URL 时：交互终端进入配置向导（repo/data_dir/remote 逐项校验并写回
  vdsh.yaml，副机跨机复制 launcher 场景的路径自愈）；非交互回退 sync.remote；
- 无参（交互菜单，内部 Read-Host）、remote / status / help（本地快）：继承 stdio 直通。
  退出码语义透传（0 成功 / 1 硬失败 / 2 用法错误 / 3 被阻塞 / 4 未初始化）。

配置经「环境桥」注入 sync-dsh.ps1（VDG_SYNC_* / VDG_ANIMATION_* / DSH_HOME）：
仅通过 vdsh 调用时生效；直接/菜单调用脚本时无这些变量 → 完全回退脚本内置默认。
"""

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

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


def _has_utf8_bom(path):
    """脚本是否带 UTF-8 BOM（sync-dsh.ps1 面向 PS 5.1，无 BOM 会按 ANSI 解码全线解析失败）。

    读失败视为通过（让脚本自己报错），不掩盖更底层的文件问题。
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return True


# ── 交互式 init（vdsh sync init 无参 + TTY）：repo / data_dir / remote ──────────────
# 副机首次接入：launcher 目录从主力机复制过来时 vdsh.yaml 残留另一台机器的路径
# （如 T:/deepseek-harness、file:///T:/DataBase/...）。此向导逐项提问、校验并写回
# vdsh.yaml，再继续脚本的 init 流程（脚本侧退出码语义不变）。

_WIZARD_RETRIES = 3


def _valid_repo_path(value):
    """校验 Harness 仓库根：目录存在且含 package.json。返回 (ok, reason)。"""
    if not value:
        return False, "路径为空"
    if not os.path.isdir(value):
        return False, "目录不存在: %s" % value
    if not os.path.isfile(os.path.join(value, "package.json")):
        return False, "不是 Harness 仓库（缺少 package.json）: %s" % value
    return True, ""


def _repo_notes(value):
    """仓库附带提示（非致命）：构建产物缺失等。"""
    notes = []
    if value and not os.path.isfile(os.path.join(value, "apps", "cli", "lib", "bin.js")):
        notes.append("该仓库尚未构建（apps/cli/lib/bin.js 缺失；vdsh 启动时会提示 build）")
    return notes


def _data_dir_notes(value):
    """数据目录附带提示（非致命）：已存在 git 仓库 / 将创建。"""
    notes = []
    expanded = os.path.normpath(os.path.expanduser(value)) if value else ""
    if expanded and os.path.exists(expanded):
        if os.path.isdir(os.path.join(expanded, ".git")):
            notes.append("该目录已是 git 仓库，将复用现有仓库")
        elif os.path.isdir(os.path.join(expanded, "sessions")) or os.path.isdir(
                os.path.join(expanded, "storages")):
            notes.append("检测到已有 DSH 数据（init 会按「已有数据」方式接入，不会覆盖）")
    else:
        notes.append("目录尚不存在，init 会先创建")
    return notes


def _valid_data_dir(value):
    """校验 DSH 数据目录：绝对路径（支持 ~）；存在时必须是目录。返回 (ok, reason)。"""
    if not value:
        return False, "路径为空"
    expanded = os.path.normpath(os.path.expanduser(value))
    if not os.path.isabs(expanded):
        return False, "必须是绝对路径（支持 ~ 写法，如 ~/.dsh）"
    if os.path.exists(expanded) and os.path.isfile(expanded):
        return False, "已存在但是文件，应为目录: %s" % expanded
    return True, ""


def _normalize_remote(raw):
    """解析远端地址：返回 (url, warnings, reason)。

    支持：file:///X:/…、file://host/share/…（UNC 形式）、/X:/…、X:\\…、
    \\\\server\\share\\…、http(s)://…。本地路径校验存在性与裸仓库形态；
    http(s) 仅做语法校验并提示不做可达性检查。
    """
    url = (raw or "").strip().strip('"').strip("'")
    if not url:
        return "", [], "地址为空"
    warnings = []
    lower = url.lower()
    if lower.startswith(("http://", "https://")):
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return "", [], "HTTP 地址缺少主机（应为 http(s)://主机/路径）"
        warnings.append("HTTP 远端不做本机可达性检查（init 的 fetch 会实际验证）")
        return url, warnings, ""

    if lower.startswith("file://"):
        parsed = urlsplit(url)
        # file://C:/…（用户漏了第三个斜杠）：盘符出现在 netloc，按盘符路径处理。
        if parsed.netloc and re.match(r"^[A-Za-z]:$", parsed.netloc):
            local = (parsed.netloc + parsed.path)
            if re.match(r"^[A-Za-z]:/", local):
                local = local.replace("/", "\\")
            url = "file:///" + local.replace("\\", "/")
        elif parsed.netloc and parsed.netloc.lower() != "localhost":
            local = "\\\\%s%s" % (parsed.netloc, parsed.path.replace("/", "\\"))
        else:
            local = parsed.path
            if re.match(r"^/[A-Za-z]:/", local):  # file:///Z:/… → Z:\…
                local = local[1:]
            local = local.replace("/", "\\")
    elif re.match(r"^[A-Za-z]:[\\/]", url):
        local = url.replace("/", "\\")
        url = "file:///" + local.replace("\\", "/")
    elif re.match(r"^/[A-Za-z]:/", url):
        local = url[1:].replace("/", "\\")
        url = "file:///" + local.replace("\\", "/")
    elif url.startswith("\\\\"):
        local = url.replace("/", "\\")
    else:
        return "", [], ("无法识别的远端格式（支持 file:///X:/…、X:\\…、"
                        "\\\\server\\share\\… 与 http(s)://…）")

    if not os.path.exists(local):
        return "", [], ("本地路径不存在: %s（副机请先映射主力机共享盘，"
                        "或确认盘符/挂载正确）" % local)
    if not os.path.isdir(local):
        return "", [], "不是目录: %s（应为 git init --bare 创建的裸仓库目录）" % local
    has_gitdir = os.path.isdir(os.path.join(local, ".git"))
    bare_shape = (os.path.isfile(os.path.join(local, "HEAD"))
                  and os.path.isdir(os.path.join(local, "objects")))
    if has_gitdir:
        warnings.append("该目录是普通仓库（含 .git/）；建议使用裸仓库（git init --bare）")
    elif not bare_shape:
        return "", [], ("目录不是裸仓库形态（缺少 HEAD/objects 等）: %s"
                        "（应为 git init --bare 创建的裸仓库目录）" % local)
    else:
        warnings.append("已确认是裸仓库（HEAD/objects/refs ✓）")
    return url, warnings, ""


def _validate_remote_input(value):
    """远端校验器（供 _prompt_value）：规范化 & 校验，返回 (ok, reason)。"""
    _url, _warnings, reason = _normalize_remote(value)
    return (not reason), reason


def _prompt_value(label, default, validator):
    """单行交互提问：回车=默认；s/skip/q=跳过；返回 (value, state)。

    state: 'ok' 校验通过 / 'skip' 用户跳过 / 'invalid' 连续无效后放弃 /
           'cancel' EOF/Ctrl+C。
    """
    prompt = "%s%s > " % (label, ("（默认: %s）" % default) if default else "")
    for _ in range(_WIZARD_RETRIES):
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None, "cancel"
        if answer.lower() in ("s", "skip", "skip;", "q"):
            return None, "skip"
        value = answer or default
        ok, reason = validator(value)
        if ok:
            return value, "ok"
        print("      ✗ %s" % reason, file=sys.stderr)
    return None, "invalid"


def _interactive_init(settings):
    """vdsh sync init 无参（TTY）交互向导：repo / data_dir / remote → 写 vdsh.yaml。

    返回远端 URL（随后交由脚本执行 init），None = 用户跳过/取消（不执行）。
    """
    print()
    print("vdsh · 数据同步初始化向导（回车=默认；s=跳过该项；Ctrl+C 取消）")
    print("      副机首次接入：填写本机 Harness 仓库、DSH 数据目录与主力机共享裸仓库地址。")

    # [1/3] repo：当前配置无效时自动改用本机探测到的仓库（跨机复制 launcher 的自愈）。
    current_repo = settings_mod.effective_repo(settings)
    default_repo = current_repo
    if not _valid_repo_path(current_repo)[0]:
        probed = settings_mod.probe_repo()
        if probed:
            default_repo = probed
            print("      ⚠ 当前配置的仓库无效（%s），默认改用本机探测到的: %s"
                  % (current_repo, probed), file=sys.stderr)
        else:
            default_repo = ""
            print("      ⚠ 当前配置的仓库无效（%s），请手动输入" % current_repo, file=sys.stderr)
    repo, state = _prompt_value("[1/3] DSH 安装目录（Harness 仓库根，含 package.json）",
                                default_repo, _valid_repo_path)
    if state == "cancel":
        print("vdsh · 已取消（未执行初始化）")
        return None
    if state == "invalid":
        print("vdsh ⚠ 连续输入无效，跳过 repo（vdsh 启动时会再检查；可稍后 vdsh setup）",
              file=sys.stderr)
        repo = None
    if repo and state == "ok":
        for note in _repo_notes(repo):
            print("      %s" % note)

    # [2/3] data_dir：默认取生效值（env>vdsh.yaml），校验绝对路径。
    current_dir = settings_mod.effective_data_dir(settings) or os.path.join(
        os.path.expanduser("~"), ".dsh")
    for note in _data_dir_notes(current_dir):
        print("      %s" % note)
    data_dir, state = _prompt_value("[2/3] DSH 数据目录（数据同步根；支持 ~）",
                                    current_dir, _valid_data_dir)
    if state == "cancel":
        print("vdsh · 已取消（未执行初始化）")
        return None
    if state == "invalid":
        print("vdsh ⚠ 连续输入无效，跳过 data_dir（保留现有配置）", file=sys.stderr)
        data_dir = None
    if data_dir and state == "ok":
        # 用户改了路径时重新按最终值提示（默认值已在上方提示过，避免重复）。
        same_path = (os.path.normpath(os.path.expanduser(data_dir))
                     == os.path.normpath(os.path.expanduser(current_dir)))
        if not same_path:
            for note in _data_dir_notes(data_dir):
                print("      %s" % note)

    # [3/3] remote：默认取配置文件（常残留主力机 T: 路径，明确提示）。
    current_remote = settings["sync"]["remote"]
    has_stale = bool(current_remote) and re.search(r"[\\/]T:[\\/]", current_remote) is not None
    if has_stale:
        print("      ⚠ 当前配置的远端是主力机路径（%s）；副机请映射共享盘后使用"
              "对应 Z: 或 UNC 地址。" % current_remote, file=sys.stderr)
    remote, state = _prompt_value("[3/3] 远端数据仓库地址（裸仓库）",
                                  current_remote, _validate_remote_input)
    if state == "cancel":
        print("vdsh · 已取消（未执行初始化）")
        return None
    if state == "invalid":
        print("vdsh ⚠ 连续输入无效，跳过 remote（保留现有配置）", file=sys.stderr)
        remote = None
    if remote and state == "ok":
        _url, warnings, _reason = _normalize_remote(remote)
        remote = _url or remote
        for warning in warnings:
            print("      %s" % warning)

    if not remote:
        print("vdsh · 未提供远端地址，初始化未执行。用法: vdsh sync init <URL>"
              "（或 vdsh sync remote set <URL> 后再运行）")
        return None

    # 写回 vdsh.yaml（文本级补丁，保留注释与其余键）。
    replacements = {}
    if repo:
        replacements[("launcher", "repo")] = settings_mod.normalize_repo(repo)
    if data_dir:
        replacements[("sync", "data_dir")] = data_dir
    replacements[("sync", "remote")] = remote
    ok_write, warnings = settings_mod.patch_values(replacements)
    for warning in warnings:
        print(warning, file=sys.stderr)
    if ok_write:
        print("vdsh · 配置已写入 vdsh.yaml（vdsh config 查看生效值）")
    else:
        print("vdsh ⚠ 配置写入失败，请手动编辑 vdsh.yaml", file=sys.stderr)
    return remote


def run_sync(sync_args, settings):
    """在当前控制台运行同步脚本，返回其退出码。

    脚本位置唯一：本目录 dsh-data-git-sync/sync-dsh.ps1；缺失时按「未初始化」(4) 处理。
    init 无 URL 时：交互终端进入配置向导（_interactive_init）；非交互回退
    vdsh.yaml 的 sync.remote，仍缺失则报用法错误（2）。
    """
    if not SYNC_PS1.is_file():
        print("vdsh ⚠ 同步脚本缺失: %s" % SYNC_PS1, file=sys.stderr)
        return SYNC_NOT_SETUP
    if not _has_utf8_bom(SYNC_PS1):
        print("vdsh ✗ 同步脚本编码异常：%s 缺少 UTF-8 BOM（Windows PowerShell 5.1 需要，"
              "否则中文乱码、全线解析失败）。请以 UTF-8 with BOM 重新保存该文件。" % SYNC_PS1,
              file=sys.stderr)
        return 1

    sub = sync_args[0].lower() if sync_args else ""
    if sub == "init":
        url = sync_args[1] if len(sync_args) > 1 else None
        if url is None:
            if sys.stdin.isatty():
                # 交互终端：无参 init → 配置向导（副机场景：repo/data_dir/remote 逐项校验）。
                url = _interactive_init(settings)
                if url is None:
                    return 0  # 用户跳过/取消，不执行 init
            else:
                url = settings["sync"]["remote"]
                if not url:
                    print("vdsh ✗ 缺少远程地址。用法: vdsh sync init <远程URL>"
                          "（或在 vdsh.yaml 配置 sync.remote；交互终端可无参运行进入配置向导）",
                          file=sys.stderr)
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
