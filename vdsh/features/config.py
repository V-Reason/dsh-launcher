# -*- coding: utf-8 -*-
"""配置查看功能：vdsh config —— 打印 vdsh.yaml 生效配置与来源标注。

报告类输出（特殊项）：不打转轮、不给结论，直接用 config_report 的分组格式。
"""

from .. import settings as settings_mod
from ..console import die, say

NAME = "config"
SUMMARY = "查看生效配置（vdsh.yaml；缺失时自动生成模板）"


def run(argv, settings):
    if argv:
        die("config 不接受参数", 2)
    say(settings_mod.config_report(settings))
    return 0
