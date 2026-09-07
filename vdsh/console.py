# -*- coding: utf-8 -*-
"""控制台交互辅助：流编码、统一文案前缀与退出码。

所有功能模块在输出用户可见信息时使用本层辅助（say/warn/die/ask），
保证文案与退出码语义一致（退出码定义见 vdsh/config.py）。
"""

import sys

from .config import EXIT_ERROR


def setup_streams():
    """统一 stdout/stderr 为 UTF-8。

    终端为 PEP 528 本就 UTF-8；重定向时避免子进程的 ✅/emoji 等字符
    在 gbk 编码下 UnicodeEncodeError。任何入口必须先调用。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def say(message=""):
    print(message)


def step(message, to_stderr=False):
    """进度/说明行（·）；to_stderr=True 时写 stderr。"""
    print("vdsh · %s" % message, file=sys.stderr if to_stderr else sys.stdout)


def warn(message):
    """警告行（⚠），写 stderr。"""
    print("vdsh ⚠ %s" % message, file=sys.stderr)


def die(message, code=EXIT_ERROR):
    """致命错误行（✗）并退出。"""
    print("vdsh ✗ %s" % message, file=sys.stderr)
    sys.exit(code)


def ask(prompt, default=None):
    """交互询问一行；空输入返回 default；EOF 返回 None（无默认时亦返回 None）。"""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer == "":
        return default
    return answer
