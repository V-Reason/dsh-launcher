# -*- coding: utf-8 -*-
"""首次配置向导功能：vdsh setup —— 随时重跑首次运行引导（见 vdsh/bootstrap.py）。"""

from .. import bootstrap
from ..console import die

NAME = "setup"
SUMMARY = "重新运行首次配置向导（DSH 安装地址等）"


def run(argv, settings):
    if argv:
        die("setup 不接受参数", 2)
    return bootstrap.run_setup()
