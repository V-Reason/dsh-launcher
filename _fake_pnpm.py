# -*- coding: utf-8 -*-
"""假 pnpm（临时复测用，不入库）：按调用记录与开关决定退出码。

单独成一个文件是为了让注入命令保持简单——`python -c` 里塞多分支会一路踩
`if/else` 与列表推导的坑（本会话实测：漏 else 会把陈旧产物指纹带到成功路径）。
放在工作区根而非 scratch 目录：scratch 每轮开始都会被清空，驱动必须常驻。
"""
import os
import sys

LOG = os.environ["VDSH_TEST_LOG"]
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\n")

STALE = ('[MISSING_EXPORT] "sanitizeProfile" is not exported by '
         '"../../packages/boot/app-boot/lib/index.js".')

if args[:2] == ["run", "clean"]:
    if os.environ.get("VDSH_TEST_CLEAN_FAIL"):
        print("clean failed: cannot remove lib")
        sys.exit(1)
    print("clean: removed 2 paths")
    sys.exit(0)

if args[:2] != ["run", "build"]:
    sys.exit(1)

with open(LOG, "r", encoding="utf-8") as handle:
    attempt = handle.read().count("run build")

if os.environ.get("VDSH_TEST_BUILD_OK"):
    print("vite v7 building for production...")
    sys.exit(0)

# 重试成功：第二次尝试（attempt == 2）且调用方声明重试应成功
if os.environ.get("VDSH_TEST_RETRY_OK") and attempt == 2:
    print("vite v7 building for production...")
    sys.exit(0)

if os.environ.get("VDSH_TEST_PLAIN_FAIL"):
    print("TS2304: Cannot find name 'x'.")
else:
    print(STALE)
print("vite v7 building for production...")
sys.exit(1)
