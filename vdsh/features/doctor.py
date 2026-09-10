# -*- coding: utf-8 -*-
"""自检功能：vdsh doctor —— 只读检查仓库/依赖/端口/配置，不改任何状态。

任何 ✗ 项 → 退出码 1；仅 ⚠ 提示 → 退出码 0。
"""

import os
import shutil

from .. import profile_state
from .. import settings as settings_mod
from ..config import CLI_REL, DIST_REL, PORT, URL
from ..console import die

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


def _removed_export_hit(lib_index):
    """插件发布产物是否仍导入已移除的 settingsNamespace（DSH 0.1.3-alpha.1，迁移指南 §1）。"""
    try:
        with open(lib_index, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    return "settingsNamespace" in text and "@deepseek-ai/dsh-settings" in text


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
    mark = {"ready": "✓", "starting": "⚠", "idle": "—", "unknown": "⚠", "auth": "✓"}.get(status, "?")
    print("  %s 端口 %d（%s）%s" % (mark, PORT, status, {
        "idle": "未运行（自检仅作参考）",
        "starting": "启动中，稍候可用",
        "ready": "服务运行中 → %s" % URL,
        "auth": "服务运行中（浏览器认证）→ 用 DSH 打印的 URL 打开",
        "unknown": "探测异常",
    }.get(status, "")))
    print("数据同步:")
    data_dir = settings_mod.effective_data_dir(settings) or os.path.join(
        os.path.expanduser("~"), ".dsh")
    failed |= not _check("数据目录: %s" % data_dir, os.path.isdir(data_dir),
                         "目录不存在（首次运行 DSH 或 vdsh sync init 时创建）")

    print("profile 插件:")
    profile_dir = os.path.join(data_dir, "profiles", "web")
    state = profile_state.verify(profile_dir, data_dir=data_dir, repo=repo)
    if not state["present"]:
        print("  — profile 未初始化（首次运行 DSH 或 vdsh sync init 后生成）")
    elif not state["plugins"]:
        _check("插件依赖", True, "无组件依赖（纯平台默认）")
    else:
        for plugin in state["plugins"]:
            name = plugin["name"]
            if not plugin["installed"]:
                continue  # 缺失由下方 failures 统一给出（含修复命令）
            installed_dir = os.path.join(profile_dir, "node_modules", name)
            if _removed_export_hit(os.path.join(installed_dir, "lib", "index.js")):
                failed |= not _check(
                    "插件 %s 已适配 DSH 平台" % name, False,
                    "仍导入已移除的 settingsNamespace：按 dsh-plugin-migration-guide 适配后重装")
                continue
            # 版本/commit 取磁盘解析身份（git 依赖看 commit），并标出生效方式：
            # profile 层 = 声明 dsh.bundle 且已激活；预设挂载/普通依赖 = 非 profile 层。
            _check("插件 %s（%s · %s）" % (name, plugin["display"], plugin["activation"]), True)
        for text in state["failures"]:
            failed = True
            print("  ✗ %s" % text)
        for text in state["warnings"]:
            print("  ⚠ %s" % text)
        if state["peer_hint"]:
            print("  — pnpm 的 peer 提示属 profile 设计预期（peer 由 Harness 安装层 "
                  "profiles/node_modules 提供）；明细：cd %s; pnpm peers check" % profile_dir)

    print()
    if failed:
        print("vdsh ✗ 存在失败项，请按上方提示处理。")
        return 1
    print("vdsh ✓ 检查完毕，环境正常。")
    return 0
