# -*- coding: utf-8 -*-
"""功能注册表：vdsh 是「多功能启动器」，每个功能一个独立模块（尽量解耦）。

功能模块约定（features/ 下的一个 .py 文件）：
  - 导出 NAME（注册名）、SUMMARY（一句话说明）、run(argv, settings) -> int；
  - 只依赖共享层（config / settings / spinner / console），不互相 import（启动
    功能按需长驱动用 build、sync 的纯函数除外）；
  - 在这里注册一行即对 CLI 可见：`vdsh <NAME> [args...]`。
"""

from . import build, config, doctor, setup, sync, update
from . import launch as launch_feature

FEATURES = {
    "build": ("build", build.SUMMARY, build.run),
    "sync": ("sync", sync.SUMMARY, sync.run),
    "update": ("update", update.SUMMARY, update.run),
    "config": ("config", config.SUMMARY, config.run),
    "setup": ("setup", setup.SUMMARY, setup.run),
    "doctor": ("doctor", doctor.SUMMARY, doctor.run),
}
DEFAULT_FEATURE = ("launch", launch_feature.SUMMARY, launch_feature.run)


def summary_lines():
    """用法输出用的功能列表。"""
    return [(name, summary) for name, (_, summary, _) in FEATURES.items()]
