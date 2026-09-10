# -*- coding: utf-8 -*-
"""pnpm 子进程输出分流：把它的噪声翻译成「一行进度」，或原样放行。

用途：`update`（pnpm install / dsh plugin update）与 `build`（pnpm run build）都把
`Noise()` 实例作为 `quiet` 回调传给 `spinner.run_child_progress`，三个去向：

  None   原样打印（真错误、构建工具输出、未知行一律保留）
  ""     静默折叠（pnpm 的统计行、成功回执、peer 提示等——解释性内容归文档）
  "→ x"  折叠该行，改打一条归一化进度行 `→ x`

进度行按种类各自节流（解析进度与网络重试是两种信号，共用一个窗口会让停滞期的
唯一反馈被挤掉）。`observed` 记录「看到过 pnpm 运行标记」，供调用方在退出码为 0
却没有任何标记时如实说明「更新可能没真正执行」。

背景（为什么这些行是噪声而不是错误）见 doc/experience.md §7.4。
"""

import re
import time

# 颜色码：管道下 pnpm 通常不着色，但 FORCE_COLOR 等会；匹配前一律剥掉。
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# (规则, 种类)：命中即折叠；progress/retry 另出进度行，silent 静默折叠。
QUIET_RULES = (
    (re.compile(r"^Progress:\s+resolved\s+(\d+),\s+reused\s+(\d+)"), "progress"),
    (re.compile(r"^\[WARN\]\s+(GET|HEAD|POST)\s+\S+\s+error\s+\(.+?\)\..*retries?\s+left\."), "retry"),
    (re.compile(r"^\[WARN\]\s+Issues with peer dependencies found"), "silent"),
    (re.compile(r"^Packages:\s*[-+]\d"), "silent"),
    (re.compile(r"^[-+]{2,}$"), "silent"),
    (re.compile(r"^Already up to date$"), "silent"),
    (re.compile(r"^Lockfile is up to date"), "silent"),
    (re.compile(r"^Done in \S+"), "silent"),
    (re.compile(r"^✓\s+Lockfile\b.*passes supply-chain policies"), "silent"),
)

# 网络类硬失败指纹：命中则把错误文案指向网络/代理（退出码语义不变）。
NETFAIL_RE = re.compile(
    r"ERR_PNPM_[A-Z_]*(?:FETCH|NETWORK|TIMEOUT)[A-Z_]*"
    r"|Seems like you have internet connection issues"
)


class Noise:
    """`quiet` 回调实现：统计进度、生成 `→` 进度行、标记 pnpm 是否真的跑过。"""

    THROTTLE_SECONDS = 3.0

    def __init__(self, throttle_seconds=None):
        self.resolved = 0
        self.reused = 0
        self.retries = 0
        self.observed = False
        self.throttle = self.THROTTLE_SECONDS if throttle_seconds is None else throttle_seconds
        self._last_at = {}
        self._last_text = {}

    def _progress(self, kind, text):
        now = time.monotonic()
        if text == self._last_text.get(kind) or \
                now - self._last_at.get(kind, 0.0) < self.throttle:
            return ""
        self._last_at[kind] = now
        self._last_text[kind] = text
        return "→ " + text

    def __call__(self, line):
        clean = ANSI_RE.sub("", line).strip()
        for pattern, kind in QUIET_RULES:
            match = pattern.match(clean)
            if match is None:
                continue
            self.observed = True
            if kind == "progress":
                self.resolved = int(match.group(1))
                self.reused = int(match.group(2))
                detail = "解析 %d" % self.resolved
                if self.reused:
                    detail += " · 复用 %d" % self.reused
                return self._progress(kind, detail)
            if kind == "retry":
                self.retries += 1
                return self._progress(kind, "网络重试 %d" % self.retries)
            return ""
        return None


def has_network_failure(lines):
    """子进程输出里是否命中网络类失败指纹。"""
    for line in lines or ():
        if NETFAIL_RE.search(ANSI_RE.sub("", line)):
            return True
    return False
