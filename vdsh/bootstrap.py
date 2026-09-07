# -*- coding: utf-8 -*-
"""首次运行引导（bootstrap / vdsh setup 共用）。

首次执行启动（launch）时，若 vdsh.yaml 不存在，提示配置 DSH 安装地址
（Harness 仓库根目录，须含 package.json）以及可选的 DSH 数据目录与
Tailscale 域名；交互被拒绝（非 TTY / EOF / Ctrl+C）时退化为生成默认模板，
不阻塞任何命令。随时可用 `vdsh setup` 重跑。
"""

import os
import sys

from .config import DEFAULT_REPO
from . import settings as settings_mod


def _valid_repo(path):
    return bool(path) and os.path.isfile(os.path.join(path, "package.json"))


def _default_repo():
    return os.environ.get("DSH_REPO") or DEFAULT_REPO


def _ask_repo():
    """询问 DSH 安装目录；返回将写入 vdsh.yaml 的值（None = 跳过不写入）。

    无效路径重试最多 3 次；仍无效则接受最后一次输入并警告（启动时会再检查）。
    """
    default = _default_repo()
    if _valid_repo(default):
        hint = "（默认有效，回车即用）"
    else:
        hint = "（默认无效或未安装 dsh，请手动输入）"
    answer = None
    for attempt in range(3):
        prompt = ("[1/3] DSH 安装目录（Harness 仓库根目录，须包含 package.json）\n"
                  "     默认: %s %s\n"
                  "     输入 s 跳过，或直接输入目录 > " % (default, hint))
        answer = _read(prompt)
        if answer is None:
            return None  # EOF/Ctrl+C：交由调用方生成默认模板
        lowered = answer.lower()
        if lowered in ("s", "skip", "skip;", "q"):
            return None
        value = answer or default
        if _valid_repo(value):
            return value
        print("vdsh ✗ 未在 %s 找到 package.json，请检查后重新输入" % value, file=sys.stderr)
    value = (answer or default).strip()
    print("vdsh ⚠ 未检测到 package.json（%s）；启动时会再次检查，可随时 vdsh setup 修改。"
          % value, file=sys.stderr)
    return value


def _read(prompt):
    try:
        value = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    return value.strip()


def _ask_data_dir():
    default = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    value = _read("[2/3] DSH 数据目录（数据同步根；缺省为 %s，直接回车）> " % default)
    if value is None:
        return None
    return value or None


def _ask_tailnet():
    value = _read("[3/3] Tailscale 域名（手机访问，可选，如 xxx.ts.net；回车跳过）> ")
    if value is None:
        return None
    value = settings_mod.normalize_tailnet(value)
    return value or None


def run_setup():
    """交互向导入口；写 vdsh.yaml（继承模板注释），返回退出码。

    非交互环境（stdin 非 TTY）→ 生成默认模板并提示；EOF/Ctrl+C → 同样降级。
    """
    if not sys.stdin.isatty():
        settings_mod.ensure_config_template()
        print("vdsh ⚠ 非交互环境：已生成默认配置 %s；"
              "请编辑其中 launcher.repo 或运行 vdsh setup 配置 DSH 安装目录。"
              % settings_mod.CONFIG_PATH, file=sys.stderr)
        return 0

    print("vdsh · 首次配置向导（Ctrl+C 可中止；随时可 vdsh setup 重跑）")
    repo = _ask_repo()
    data_dir = _ask_data_dir()
    tailnet = _ask_tailnet()

    settings_mod.ensure_config_template()
    replacements = {}
    if repo is not None:
        replacements[("launcher", "repo")] = repo
    if data_dir:
        replacements[("sync", "data_dir")] = data_dir
    if tailnet:
        replacements[("launcher", "tailnet")] = tailnet

    if replacements:
        ok, warnings = settings_mod.patch_values(replacements)
        for warning in warnings:
            print(warning, file=sys.stderr)
        if ok:
            print("vdsh · 配置已写入 %s（vdsh config 查看生效值）" % settings_mod.CONFIG_PATH)
        else:
            print("vdsh ⚠ 配置写入失败，请手动编辑 %s" % settings_mod.CONFIG_PATH, file=sys.stderr)
    else:
        print("vdsh · 已使用默认配置 %s（启动时如仓库无效会再次提示）" % settings_mod.CONFIG_PATH)
    return 0
