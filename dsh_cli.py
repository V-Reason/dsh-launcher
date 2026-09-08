#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""dsh_cli.py — 官方 DSH CLI 转发壳（dsh.cmd 调用）。

把 `dsh` 命令转发到本机的 Harness CLI（apps/cli/lib/bin.js），不再硬编码某台
机器的仓库路径（旧 dsh.cmd 写死 T:\deepseek-harness\...，副机报 MODULE_NOT_FOUND）。
按以下顺序解析仓库根目录：

  1. 环境变量 DSH_REPO（显式指定，优先级最高）；
  2. vdsh.yaml 的 launcher.repo（与 vdsh 启动器共用配置）；
  3. 本机候选路径（C:\deepseek-harness、T:\deepseek-harness，第一个含 package.json 者）。

全部无效 → 提示 vdsh setup / DSH_REPO；仓库有效但 CLI 产物缺失 → 提示先 vdsh build。
其余参数原样转发给 node，退出码透传。仅用标准库（文本级读 vdsh.yaml，复用
sync-dsh.ps1 的 Get-VdgConfigRemote 式解析，不引入 pyyaml）。
"""

import json
import os
import re
import shutil
import subprocess
import sys

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_REL = "apps/cli/lib/bin.js"
CONFIG_PATH = os.path.join(LAUNCHER_DIR, "vdsh.yaml")
REPO_CANDIDATES = [r"C:\deepseek-harness", r"T:\deepseek-harness"]


def _valid_repo(path):
    return bool(path) and os.path.isfile(os.path.join(path, "package.json"))


def _config_value(key):
    """从 vdsh.yaml 读顶层键值（支持 JSON 双引号值与裸值+行尾注释）。"""
    if not os.path.isfile(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r"(?m)^\s+%s:\s*(.+)$" % re.escape(key), text)
    if not match:
        return None
    # 剥行尾注释（模板/手工配置常在值后跟 `# 说明`），再去引号/JSON 反解。
    value = re.split(r"[ \t]+#", match.group(1).strip(), maxsplit=1)[0].rstrip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value.strip('"')
    return value or None


def _repo_from_config():
    """从 vdsh.yaml 读取 launcher.repo（支持 JSON 双引号值与裸值+行尾注释）。"""
    return _config_value("repo")


def _data_dir():
    """DSH 数据目录：DSH_HOME 环境变量 > vdsh.yaml sync.data_dir > ~/.dsh。"""
    env = os.environ.get("DSH_HOME", "").strip()
    if env:
        return os.path.normpath(os.path.expanduser(env))
    configured = _config_value("data_dir")
    if configured:
        return os.path.normpath(os.path.expanduser(configured))
    return os.path.normpath(os.path.expanduser("~/.dsh"))


def _resolve():
    """返回 (repo, source)；source 用于错误提示。"""
    env = os.environ.get("DSH_REPO", "").strip()
    if env:
        return env, "环境变量 DSH_REPO"
    configured = _repo_from_config()
    if configured and _valid_repo(configured):
        return configured, "vdsh.yaml 的 launcher.repo"
    for candidate in REPO_CANDIDATES:
        if _valid_repo(candidate):
            return candidate, "本机候选路径（建议运行 vdsh setup 固化到 vdsh.yaml）"
    return None, None


def main():
    argv = sys.argv[1:]
    repo, source = _resolve()
    if not repo:
        print("dsh ✗ 未找到 Harness 仓库。请运行 vdsh setup 填写 DSH 安装目录，"
              "或设置环境变量 DSH_REPO（示例: set DSH_REPO=C:\\deepseek-harness）。",
              file=sys.stderr)
        return 1
    node = shutil.which("node")
    if node is None:
        print("dsh ✗ 未找到 node（Node.js），请安装并加入 PATH。", file=sys.stderr)
        return 1
    cli = os.path.join(repo, CLI_REL)
    if not os.path.isfile(cli):
        print("dsh ✗ 未找到 dsh CLI 产物: %s" % cli, file=sys.stderr)
        print("      仓库来源: %s" % source, file=sys.stderr)
        print("      若仓库无误请先构建（vdsh build）；若路径不对请 vdsh setup 重新配置。",
              file=sys.stderr)
        return 1
    # 模块回退自愈：git 同步把 junction 展开成真实目录/文件后，dsh 启动会报
    # 「exists and is not a symlink or dsh-managed module proxy」；启动前清掉污染条目。
    try:
        from vdsh.module_fallback import heal_module_fallback
        healed = heal_module_fallback(_data_dir())
        if healed > 0:
            print("dsh ⚠ 已清理 %d 个模块回退污染条目（.dsh-module-fallback 下的真实目录/文件，"
                  "dsh 启动时将自动重建）" % healed, file=sys.stderr)
    except Exception as error:  # 自愈失败不阻断启动（dsh 自身的报错会给出指引）
        print("dsh ⚠ 模块回退自愈跳过（%s）" % error, file=sys.stderr)
    try:
        return subprocess.call([node, cli] + argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
