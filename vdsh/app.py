# -*- coding: utf-8 -*-
"""vdsh_launcher 主入口：控制台设置 → 首次运行引导 → 配置加载 → 功能分发。

架构（功能尽量解耦）：
  共享层  config / settings / spinner / console / bootstrap
  功能层  features/（launch / build / sync / config / setup / doctor）
  分发器  本文件：特征名 → FEATURES 注册表；未命中 → 默认 launch
"""

import sys

try:
    import requests  # noqa: F401  （保留旧行为：缺依赖在导入时给出友好提示）
except ImportError:
    print("vdsh ✗ 请执行 pip install requests", file=sys.stderr)
    sys.exit(4)

from . import bootstrap, cli, console, settings as settings_mod, spinner
from .config import CONFIG_PATH
from .features import DEFAULT_FEATURE, FEATURES


def _load_config():
    """加载 vdsh.yaml（缺失时生成模板），打印警告，配置动画全局参数。

    返回 (settings, warnings)。
    """
    settings_mod.ensure_config_template()
    settings, warnings = settings_mod.load_settings()
    for warning in warnings:
        print(warning, file=sys.stderr)
    spinner.configure(
        fps=settings["animation"]["fps"],
        frames=settings["animation"]["frames"],
        quiet=settings["animation"]["quiet"],
    )
    return settings, warnings


def main():
    console.setup_streams()
    argv = sys.argv[1:]

    # 帮助仅在显式请求时显示（vdsh help / -h / --help）；裸 `vdsh` = 默认启动功能。
    if argv and argv[0] in ("-h", "--help", "help"):
        cli.print_usage()
        sys.exit(0)

    feature_key = argv[0] if argv and argv[0] in FEATURES else None

    if feature_key is None:
        # 默认功能（launch）：首次运行 → 交互向导（非交互/跳过 → 默认模板 + 提示）。
        if not CONFIG_PATH.exists():
            bootstrap.run_setup()
        settings, _warnings = _load_config()
        runner = DEFAULT_FEATURE[2]
        sys.exit(runner(argv, settings))

    settings, _warnings = _load_config()
    runner = FEATURES[feature_key][2]
    sys.exit(runner(argv[1:], settings))


if __name__ == "__main__":
    main()
