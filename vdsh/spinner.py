# -*- coding: utf-8 -*-
"""加载动画（与 vdsh 启动同风格）与「子进程 + 动画」执行器。

动画：TTY 下原地重绘转轮帧 + 消息 + 已等待秒数（8fps，独立 daemon 线程）；
非 TTY 自动静默，流式输出不受影响。经锁 + 暂停事件与主线程（子进程行输出）
协作，动画行与数据行互不覆盖。
"""

import subprocess
import sys
import threading
import time

# 动画全局默认（app 启动时经 configure() 用 vdsh.yaml 覆盖）。
_FPS = 8
_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def configure(fps=None, frames=None):
    """用 vdsh.yaml 的 animation.* 覆盖动画全局默认（非法值忽略，保持原值）。"""
    global _FPS, _FRAMES
    if isinstance(fps, int) and 1 <= fps <= 60:
        _FPS = fps
    if isinstance(frames, str) and len(frames) >= 2:
        _FRAMES = tuple(frames)


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


def run_child_progress(command, message, cwd=None, env=None, timeout=None):
    """运行子进程并流式显示其输出；等待期间显示与启动同风格的动画。

    stdout/stderr 合并为 UTF-8 文本逐行透传（errors=replace 兼容任意字节）；
    模拟字符串的子进程（PowerShell 脚本）会自行将输出编码为 UTF-8。
    timeout（秒）>0 时：超时终止子进程树（taskkill /T /F）并返回 1（打印超时提示）。
    返回子进程退出码；KeyboardInterrupt（Ctrl+C）时终止子进程并重抛。
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
    try:
        for line in proc.stdout:
            spinner.say(line.rstrip("\r\n"))
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
        print("vdsh ⚠ 超过 %s 秒未完成，已终止（远端可能不可达）。" % timeout)
        return 1
    return proc.returncode
