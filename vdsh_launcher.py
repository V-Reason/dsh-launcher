#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vdsh_launcher.py — DeepSeek Harness Web 启动器入口（薄壳）。

实现已拆分到 `vdsh/` 包（模块职责见 vdsh/__init__.py）；
本文件仅负责把脚本所在目录加入 sys.path 并转发 main()，保证 vdsh.cmd
与本目录下直接 `python vdsh_launcher.py` 的行为与单文件版本一致。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdsh.app import main  # noqa: E402

if __name__ == "__main__":
    main()
