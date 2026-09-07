# -*- coding: utf-8 -*-
"""自检功能：vdsh doctor —— 只读检查仓库/依赖/端口/配置，不改任何状态。

任何 ✗ 项 → 退出码 1；仅 ⚠ 提示 → 退出码 0。
"""

import os
import shutil

from .. import settings as settings_mod
from ..config import CLI_REL, DIST_REL, PORT, URL
from ..console import die, say

NAME = "doctor"
SUMMARY = "环境自检：仓库/依赖/端口/配置（只读）"


def _check(label, ok, hint=""):
    mark = "✓" if ok else "✗"
    if hint and not ok:
        label = "%s （%s）" % (label, hint)
    print("  %s %s" % (mark, label))
    return ok


def _check_tool(label, name):
    return _check(label, shutil.which(name) is not None, "未在 PATH 找到 %s" % name)


def run(argv, settings):
    if argv:
        die("doctor 不接受参数", 2)
    from . import launch as launch_mod  # 延迟导入（probe_harness）

    failed = False
    print("vdsh 环境自检（只读）")

    print("配置:")
    if settings_mod.CONFIG_PATH.exists():
        _check("vdsh.yaml 已存在（%s）" % settings_mod.CONFIG_PATH.parent, True)
    else:
        _check("vdsh.yaml 存在", False, "下次运行自动生成模板")

    print("DSH 仓库:")
    repo = settings_mod.effective_repo(settings)
    failed |= not _check("仓库根目录: %s" % repo, os.path.isdir(repo), "目录不存在")
    if os.path.isdir(repo):
        failed |= not _check("package.json", os.path.isfile(os.path.join(repo, "package.json")))
        failed |= not _check("CLI 产物 %s" % CLI_REL, os.path.isfile(os.path.join(repo, CLI_REL)),
                             "需执行构建")
        failed |= not _check("Web 产物 %s" % DIST_REL, os.path.isfile(os.path.join(repo, DIST_REL)),
                             "需执行构建")

    print("工具链:")
    failed |= not _check_tool("python", "python")
    failed |= not _check_tool("node", "node")
    failed |= not _check_tool("pnpm", "pnpm")
    failed |= not _check_tool("pwsh（PowerShell 7）", "pwsh")
    failed |= not _check_tool("powershell（同步宿主 5.1）", "powershell")
    failed |= not _check_tool("git", "git")

    print("运行状态:")
    try:
        status = launch_mod.probe_harness()
    except Exception as error:  # 网络/requests 异常不可靠时仅提示
        status = "unknown"
        print("  ⚠ 端口探测失败：%s" % error)
    mark = {"ready": "✓", "starting": "⚠", "idle": "—", "unknown": "⚠"}.get(status, "?")
    print("  %s 端口 %d（%s）%s" % (mark, PORT, status, {
        "idle": "未运行（自检仅作参考）",
        "starting": "启动中，稍候可用",
        "ready": "服务运行中 → %s" % URL,
        "unknown": "探测异常",
    }.get(status, "")))
    print("数据同步:")
    data_dir = settings_mod.effective_data_dir(settings) or os.path.join(
        os.path.expanduser("~"), ".dsh")
    failed |= not _check("数据目录: %s" % data_dir, os.path.isdir(data_dir),
                         "目录不存在（首次运行 DSH 或 vdsh sync init 时创建）")

    print()
    if failed:
        print("vdsh ✗ 存在失败项，请按上方提示处理。")
        return 1
    print("vdsh ✓ 检查完毕，环境正常。")
    return 0
