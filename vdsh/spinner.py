# -*- coding: utf-8 -*-
"""加载动画（与 vdsh 启动同风格）与「子进程 + 动画」执行器。

动画：TTY 下原地重绘转轮帧 + 消息 + 已等待秒数（8fps，独立 daemon 线程）；
非 TTY 自动静默，流式输出不受影响。经锁 + 暂停事件与主线程（子进程行输出）
协作，动画行与数据行互不覆盖。

子进程输出分流（`run_child_progress`），三个去向由调用方的 quiet 回调决定：
  - 原样打印：真错误/未知输出一律保留；
  - 静默折叠：统计行、成功回执等噪声；
  - 归一化进度行（`→ …`）：把噪声翻译成一句人读的进度，转轮文案保持「任务性质」不变。
  `collect` 把每一行交给调用方（TTY/非 TTY 都生效）；`replay_on_failure` 让折叠行在
  子进程非 0 退出时原样补打，保证「输出瘦身」永不吞掉失败证据；非 TTY 恒为全量透传。
"""

import collections
import subprocess
import sys
import threading
import time

from .console import warn

# 动画全局默认（app 启动时经 configure() 用 vdsh.yaml 覆盖）。
_FPS = 8
_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# TTY 下是否折叠子进程低价值行（vdsh.yaml animation.quiet；false = 排障时看全量）。
_QUIET = True

# 折叠行的环形缓冲上限（失败补打用；够覆盖 pnpm 一轮噪声，且不占内存）。
MAX_FOLDED_LINES = 200


def configure(fps=None, frames=None, quiet=None):
    """用 vdsh.yaml 的 animation.* 覆盖动画全局默认（非法值忽略，保持原值）。"""
    global _FPS, _FRAMES, _QUIET
    if isinstance(fps, int) and 1 <= fps <= 60:
        _FPS = fps
    if isinstance(frames, str) and len(frames) >= 2:
        _FRAMES = tuple(frames)
    if isinstance(quiet, bool):
        _QUIET = quiet


def _kill_tree(proc):
    """终止子进程及其进程树（Windows 用 taskkill /T /F，失败回退 kill）。"""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False)
            if result.returncode == 0:
                return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


class Spinner:
    """转轮加载动画：帧 + 消息 + 秒数；支持暂停/恢复与逐行输出版（say）。

    帧序列与帧率取模块级全局（可由 configure() 按 vdsh.yaml 覆盖）。
    """

    ERASE_WIDTH = 120  # 固定宽度擦除，兼容 CJK 双宽字符

    def __init__(self, message):
        self.message = message
        self.frames = _FRAMES
        self.fps = _FPS
        self._frame = 0
        self._started = time.monotonic()
        self._enabled = sys.stdout.isatty()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    @property
    def enabled(self):
        return self._enabled

    def start(self):
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        interval = 1.0 / self.fps
        next_tick = time.monotonic() + interval
        while True:
            remaining = next_tick - time.monotonic()
            if remaining > 0 and self._stop.wait(remaining):
                return
            with self._lock:
                if self._paused.is_set() or self._stop.is_set():
                    # 暂停/停止期间不绘制；重新对齐相位，恢复后避免补帧。
                    next_tick = time.monotonic() + interval
                    continue
                # 落后（写开销/调度抖动）时立即补帧，实际帧率贴住 FPS。
                self._frame += 1
                elapsed = int(time.monotonic() - self._started)
                sys.stdout.write("\r%s %s %ds" % (
                    self.frames[self._frame % len(self.frames)], self.message, elapsed))
                sys.stdout.flush()
            next_tick += interval

    def _erase(self):
        sys.stdout.write("\r" + " " * self.ERASE_WIDTH + "\r")
        sys.stdout.flush()

    def set_message(self, text):
        """只更新转轮文案（不擦行、不打印）；与 say()/finish() 经同一把锁串行。

        供子进程低价值输出（进度/重试计数等）驱动动画行，取代逐行打印。
        """
        with self._lock:
            self.message = text

    def say(self, line):
        """打印一行子进程输出，动画让位到下一行继续（不覆盖该行）。

        时序：先置暂停 → 持锁擦除当前动画行并打印 line + 换行 → 解除暂停；
        动画循环持锁时检查暂停态，保证与主线程写入串行化，无竞态。
        步骤行（以「→ 」开头）会同步刷新动画消息，长等待期间转轮显示当前动作。
        """
        if not self._enabled:
            if line.startswith("→ "):
                self.message = line[2:].strip()
            print(line)
            return
        self._paused.set()
        with self._lock:
            if line.startswith("→ "):
                self.message = line[2:].strip()
            self._erase()
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        self._paused.clear()

    def finish(self, text=None):
        if self._enabled:
            self._stop.set()
            with self._lock:
                self._erase()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=1.0)
        if text:
            print(text)


def run_child_progress(command, message, cwd=None, env=None, timeout=None,
                       collect=None, quiet=None, replay_on_failure=True):
    """运行子进程并流式显示其输出；等待期间显示与启动同风格的动画。

    stdout/stderr 合并为 UTF-8 文本逐行透传（errors=replace 兼容任意字节）；
    模拟字符串的子进程（PowerShell 脚本）会自行将输出编码为 UTF-8。
    timeout（秒）>0 时：超时终止子进程树（taskkill /T /F）并返回 1（打印超时提示）。
    返回子进程退出码；KeyboardInterrupt（Ctrl+C）时终止子进程并重抛。

    collect：可选 list，逐行追加已 rstrip 的输出（TTY 与非 TTY 都收集）。
    quiet：可选 callable(line) -> str | None，逐行调用（可带计数副作用），返回值决定该行去向：
        None  = 原样打印（真错误/未知输出一律保留）；
        ""    = 静默折叠（TTY 下不打印）；
        "文本" = 折叠原行，改打一条归一化进度行（调用方自带 `→ ` 前缀）；打印后转轮文案
                 **恢复为 message（任务性质）**，不跟着 pnpm 内部计数漂移。
        非 TTY 忽略折叠（保持全量透传与日志完整），但 quiet 仍会被调用以便计数。
    replay_on_failure：True（默认）时，被折叠的行在子进程非 0 退出后原样补打，
        保证瘦身不吞掉失败证据。
    """
    spinner = Spinner(message)
    spinner.start()
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    timed_out = {"value": False}
    if timeout is not None and timeout > 0:
        def _watch():
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                timed_out["value"] = True
                _kill_tree(proc)
        threading.Thread(target=_watch, daemon=True).start()
    folded = collections.deque(maxlen=MAX_FOLDED_LINES) if replay_on_failure else None
    try:
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            if collect is not None:
                collect.append(text)
            hint = None if quiet is None else quiet(text)  # 始终调用：计数副作用
            if hint is None or not (_QUIET and spinner.enabled):
                spinner.say(text)
                continue
            if folded is not None:
                folded.append(text)
            if hint:
                spinner.say(hint)             # 归一化进度行（→ …）
                spinner.set_message(message)  # 转轮回到任务性质，不显示 pnpm 内部计数
    except KeyboardInterrupt:
        # Windows 控制台的 Ctrl+C 会发给整个控制台进程组（子进程同样收到）；
        # 这里兜底确保子进程（树）终止，避免残留。
        _kill_tree(proc)
        raise
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    proc.wait()
    spinner.finish()
    if timed_out["value"]:
        warn("超过 %s 秒未完成，已终止（远端可能不可达）" % timeout)
        return 1
    code = proc.returncode
    if code != 0 and folded:
        # 失败时补打被折叠行：瘦身只针对成功路径，排查信息一条不丢。
        warn("已折叠的子进程输出（%d 行，供排查）：" % len(folded))
        for line in folded:
            print(line)
    return code
