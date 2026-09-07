# -*- coding: utf-8 -*-
"""用法输出（vdsh --help）。"""

from .config import DEFAULT_REPO
from .features import FEATURES, DEFAULT_FEATURE


def print_usage():
    print("vdsh — DeepSeek Harness Web 多功能启动器（dsh 插件做不到的事）")
    print()
    print("用法：")
    print("  vdsh [工作目录]                    [默认功能] 启动服务并打开浏览器")
    print("      --tailnet xxx.ts.net          手机经 Tailscale 访问（自动加 --trusted-host）")
    print("      --sync                        启动服务前自动拉取 DSH 数据（sync pull）")
    print("  vdsh <功能> [参数...]              调用对应功能（下表）")
    print()
    print("功能：")
    default_name, default_summary, _ = DEFAULT_FEATURE
    print("  %-8s %s（默认，无参数时）" % (default_name, default_summary))
    for name, (_, summary, _) in FEATURES.items():
        print("  %-8s %s" % (name, summary))
    print()
    print("示例：")
    print("  vdsh                          # 启动 dsh web 并打开浏览器（当前目录）")
    print("  vdsh sync push                # 收工前把 DSH 数据推送到仓库")
    print("  vdsh sync init file:///Z:/DataBase/dsh-sync-repo.git")
    print("  vdsh setup                    # 重新运行首次配置向导")
    print("  vdsh doctor                   # 环境自检")
    print("  vdsh config                   # 查看生效配置")
    print()
    print("配置：vdsh.yaml（launcher 目录下；首次运行自动生成/向导）。")
    print("环境变量：DSH_REPO（Harness 仓库根目录，默认 %s）、DSH_TAILNET_HOST（未传 --tailnet 时生效）、"
          "DSH_HOME（DSH 数据目录，默认 ~/.dsh）" % DEFAULT_REPO)
