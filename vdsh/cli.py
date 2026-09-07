# -*- coding: utf-8 -*-
"""用法输出（vdsh help / -h / --help）：精简版，只保留入口、选项与功能清单。"""

from .features import FEATURES


def print_usage():
    print("vdsh — DeepSeek Harness Web 启动器")
    print()
    print("用法：")
    print("  vdsh [选项] [工作目录]             默认功能：启动 dsh web 并打开浏览器")
    print("  vdsh <功能> [参数...]              执行对应功能（下表）")
    print("  vdsh help | -h | --help            显示本帮助")
    print()
    print("选项（仅默认功能）：")
    print("  --tailnet <域名>                   手机访问（自动加 --trusted-host）")
    print("  --sync                             启动前自动拉取 DSH 数据")
    print()
    print("功能：")
    for name, (_, summary, _) in FEATURES.items():
        print("  %-8s %s" % (name, summary))
    print()
    print("配置：vdsh.yaml（vdsh config 查看生效值）・ 环境变量：DSH_REPO / DSH_TAILNET_HOST / DSH_HOME")
