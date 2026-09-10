# -*- coding: utf-8 -*-
"""加载动画（与 vdsh 启动同风格）与「子进程 + 动画」执行器。

动画：TTY 下原地重绘转轮帧 + 消息 + 已等待秒数（8fps，独立 daemon 线程）；
非 TTY 自动静默，流式输出不受影响。经锁 + 暂停事件与主线程（子进程行输出）
协作，动画行与数据行互不覆盖。

子进程输出分流（`run_child_progress`），三个去向由调用方的 quiet 回调决定：
  - 原样打印：真错误/未知输出一律保留；
  - 静默折叠：统计行、成功回执等噪声；
  - 归一化进度行（`→ …`）：把噪声翻译成一句人读的进度，转轮文案保持「任务性质」不变。
  `collect` 把每一行交给调用方（TTY/非 TTY 都生效）；`tail_out` 额外提供「含被折叠行」的
  完整行序（调用方在失败时复述末尾若干行）；`replay_on_failure` 让折叠行在子进程非 0 退出时
  原样补打（走 stderr），保证「输出瘦身」永不吞掉失败证据；非 TTY 恒为全量透传。

挂死与误报兜底：读循环由独立线程 + 队列驱动，`STALL_SECONDS` 秒完全没有输出即按卡死终止
并返回 1（子进程退出但后代仍持有 stdout 写端时不会永久挂住）；命令无法启动返回
`EXIT_SPAWN_FAILED` 并打印一句 `vdsh ⚠`，不抛 traceback（退出码语义仍由调用方 die 决定）。
"""

import collections
import queue
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

# 子进程「连续多久没有任何输出」视为卡死（秒）：仅在进程仍在运行且完全静默时触发
# （改前 for line in proc.stdout 只在 EOF 结束 → 后代抱着写端时 vdsh 会无限挂住；
#  现在那种情形由「进程已退出即收尾」处理，这里是第二道兜底）。
STALL_SECONDS = 600

# Popen 无法启动命令（缺失/无权/不可执行）时的返回码：取 shell 的 127 语义，
# 与「子进程自己非 0 退出」区分开；调用方据返回值自行 die(code=...)。
EXIT_SPAWN_FAILED = 127


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
                       collect=None, quiet=None, replay_on_failure=True, tail_out=None):
    """运行子进程并流式显示其输出；等待期间显示与启动同风格的动画。

    stdout/stderr 合并为 UTF-8 文本逐行透传（errors=replace 兼容任意字节）；
    模拟字符串的子进程（PowerShell 脚本）会自行将输出编码为 UTF-8。
    返回子进程退出码；KeyboardInterrupt（Ctrl+C）时终止子进程并重抛。

    timeout（秒）>0 时：超时终止子进程树（taskkill /T /F）并返回 1（打印超时提示）。
    STALL_SECONDS 秒内完全没有输出同样按卡死终止并返回 1（进程仍在跑却不再吐字的情形）；
    「直系子进程已退出、但后代仍抱着 stdout 写端」不再等 EOF：取走队列中已有的行即收尾，
    并打一句 `vdsh ⚠` 说明后续输出不再等待（见模块常量注释）。
    命令无法启动（FileNotFoundError/OSError）时打印明确诊断并返回 EXIT_SPAWN_FAILED
    （127），不抛 traceback。

    collect：可选 list，逐行追加已 rstrip 的输出（TTY 与非 TTY 都收集）。
    tail_out：可选 list，追加**每一行**输出（含被折叠行，即完整 tail 语料）——给调用方
        在失败时复述末尾若干行用；长度不设限（调用方自行截断），成功任务不必消费。
        **传了它就由调用方负责呈现失败输出**，本函数不再补打折叠行（避免同一批行出现两次）。
    quiet：可选 callable(line) -> str | None，逐行调用（可带计数副作用），返回值决定该行去向：
        None  = 原样打印（真错误/未知输出一律保留）；
        ""    = 静默折叠（TTY 下不打印）；
        "文本" = 折叠原行，改打一条归一化进度行（调用方自带 `→ ` 前缀）；打印后转轮文案
                 **恢复为 message（任务性质）**，不跟着 pnpm 内部计数漂移。
        非 TTY 忽略折叠（保持全量透传与日志完整），但 quiet 仍会被调用以便计数。
    replay_on_failure：True（默认）时，被折叠的行在子进程非 0 退出后原样补打，
        保证瘦身不吞掉失败证据（走 console→stderr，stdout 被重定向时依然可见）。
    """
    spinner = Spinner(message)
    spinner.start()
    # 子进程输出在独立线程里阻塞读，主线程用队列 + 空闲判定消费：
    # 既能实时显示，也能在「没有 EOF 但有输出在流」与「彻底静默」之间做出区分。
    lines = queue.Queue()
    try:
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
    except (FileNotFoundError, OSError) as error:
        spinner.finish()
        warn("无法启动命令（%s）：%s" % (_command_text(command), error))
        return EXIT_SPAWN_FAILED
    reader_done = threading.Event()
    reader = threading.Thread(target=_pump_lines, args=(proc.stdout, lines, reader_done),
                              daemon=True)
    reader.start()
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
    stalled = {"value": False}
    abandoned = {"value": False}
    last_output = time.monotonic()
    try:
        while True:
            try:
                item = lines.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    # 子进程已退出：把已经在队列里的行全部取走再收尾。
                    # 判据必须落在「进程已退出」而不是「等到 EOF」——Windows 上管道 EOF
                    # 与进程退出没有严格时序保证，读线程可能永远等不到（旧实现
                    # `for line in proc.stdout` 正是因此会永久挂住）。
                    for text in _drain(lines):
                        _emit(text, collect, tail_out, quiet, spinner, folded, message)
                    abandoned["value"] = not reader_done.is_set()
                    break
                if time.monotonic() - last_output >= STALL_SECONDS:
                    stalled["value"] = True
                    _kill_tree(proc)
                    break
                continue
            last_output = time.monotonic()
            _emit(item, collect, tail_out, quiet, spinner, folded, message)
            if reader_done.is_set() and lines.unfinished_tasks == 0:
                break
    except KeyboardInterrupt:
        # Windows 控制台的 Ctrl+C 会发给整个控制台进程组（子进程同样收到）；
        # 这里兜底确保子进程（树）终止，避免残留。
        _kill_tree(proc)
        raise
    finally:
        _release_stream(proc, reader, reader_done)
    proc.wait()
    spinner.finish()
    if abandoned["value"]:
        # 直系子进程已退出、但 stdout 写端仍被后代持有（EOF 等不到）。已取走队列中已有的行，
        # 之后不再等待——否则 vdsh 会被一个它管不到的句柄无限期拖住。
        warn("子进程已退出，但其后代仍占用输出句柄：后续输出不再等待（可能不完整）")
    if stalled["value"]:
        warn("超过 %s 秒无任何输出，已终止（子进程可能卡死或句柄被占用）" % STALL_SECONDS)
        return 1
    if timed_out["value"]:
        warn("超过 %s 秒未完成，已终止（远端可能不可达）" % timeout)
        return 1
    code = proc.returncode
    if code != 0 and folded and tail_out is None:
        # 失败时补打被折叠行：瘦身只针对成功路径，排查信息一条不丢（stderr，重定向 stdout 也可见）。
        # 传了 tail_out 的调用方拿的是**含折叠行的完整行序**，由它自己复述末尾若干行——
        # 此时不再补打，避免同一批行出现两次。
        warn("已折叠的子进程输出（%d 行，供排查）：" % len(folded))
        for line in folded:
            print(line, file=sys.stderr)
    return code


def _emit(text, collect, tail_out, quiet, spinner, folded, message):
    """一行子进程输出的唯一出口：收集 → 分流（透传 / 折叠 / 归一化进度行）。"""
    if collect is not None:
        collect.append(text)
    if tail_out is not None:
        tail_out.append(text)
    hint = None if quiet is None else quiet(text)  # 始终调用：计数副作用
    if hint is None or not (_QUIET and spinner.enabled):
        spinner.say(text)
        return
    if folded is not None:
        folded.append(text)
    if hint:
        spinner.say(hint)             # 归一化进度行（→ …）
        spinner.set_message(message)  # 转轮回到任务性质，不显示 pnpm 内部计数


def _drain(lines):
    """非阻塞取走队列里剩余的行（子进程已退出、读线程可能仍在收尾时用）。"""
    rest = []
    while True:
        try:
            rest.append(lines.get_nowait())
        except queue.Empty:
            return rest


def _release_stream(proc, reader, reader_done):
    """等读线程收尾后再关流，避免关流把整个函数挂住。

    读线程阻塞在 BufferedReader 上（持缓冲区锁）时，主线程的 `stdout.close()` 会等同一把锁
    ——若后代进程仍持有写端不放，close() 就会一直等下去（「有卡死检测却仍然挂住」的真因）。
    因此：先 join 一个有界时间；读线程没退出就干脆不 close（daemon 线程，随进程回收）。
    """
    reader.join(timeout=2.0)
    if not reader_done.is_set():
        return
    try:
        proc.stdout.close()
    except Exception:
        pass


def _pump_lines(stream, lines, done):
    """读线程：把子进程 stdout 逐行塞进队列（每行计一个未完成任务），读完置 done。

    不投放「哨兵」行：哨兵会排在主循环尚未取走的若干行之后，遇上瞬退的子进程
    就成了「提前收尾 + 丢输出」。收尾改由主循环按 `done` + 未完成任务数判断。
    """
    try:
        for line in stream:
            lines.put(line.rstrip("\r\n"))
    except Exception:
        pass
    finally:
        done.set()


def _command_text(command):
    """命令的人类可读形式（供启动失败诊断）。"""
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)
