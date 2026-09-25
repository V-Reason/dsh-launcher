# -*- coding: utf-8 -*-
"""构建功能：检测仓库构建产物与源码时效，执行 pnpm run build（带动画）。

插件做不到的事：构建发生在 DSH 进程之外（仓库侧文件与 pnpm），
launcher 在启动前完成时效检测并按需构建。

输出遵循同一标准：转轮 = 任务性质 + 秒数，pnpm 自身噪声折叠成 `→ …` 进度行
（构建工具的真实输出照常透传），结束 `vdsh ✓ 构建完成` + 单独一行耗时。

失败诊断（不受输出简约约束）：子进程输出全程落盘到 `config.BUILD_LOG_PATH`，
非 0 退出时复述**末尾 15 行**（构建工具的真报错都在尾部）+ 完整日志路径，
并说明「代码已更新、仅构建失败」，避免只有一句 exit code 的「无证据失败」。

**陈旧产物自愈（2026-09-23，接 devlog 2026-09-10 第三波「真实失败原因不可复原」）**：
`git pull` 跨版本更新后，仓库的**增量**构建可能不重刷 `lib/`（实测：`apps/desktop/lib`
仍导入 settings 包已删除的 `SettingsProvider`，`app-boot/lib/index.js` 缺
`removeLinkProjections`/`sanitizeProfile`），tsdown 于是 `MISSING_EXPORT` 构建失败；
而 `pnpm run build` 本身不清缓存，重跑与「手动 pnpm install && pnpm run build」都无效——
当时的出路只有删库重下。现在：失败且命中陈旧产物指纹 → `pnpm run clean`
（`tsx scripts/clean.ts`，删除各包 `lib/` 与 `*.tsbuildinfo`）+ 重建一次；
`--clean` 直接全量重建，`--no-retry` 只跑一次。构建成功后写构建基线
（`lib/.vdsh-build.json` 记 HEAD），`build_needed` 据此发现「拉过代码但没重建」——
改前只比对 `apps/cli/src`、`apps/web/src` 的 mtime，其他包的源码更新一律漏检。

**启动侧误报修正（2026-09-23 当日第二轮）**：上线后第一次真机启动就暴露两个问题——
用户**没动过 DSH**（重下重建过、产物齐全）却被问「构建产物缺失/源码更新」，答 `n` 之后
`vdsh` 直接 `已取消` 退出，只能手动 `pnpm dsh web`。根因两条：
(1) `build_needed()` 把「无基线」直接当「需构建」，而**手动 `pnpm run build` 的检出本来就没有基线**；
(2) 拒绝构建 = 取消启动，而产物齐全时启动本来是能成的。
现在：判定改为 `build_reason()` 的证据递进（产物缺失 → 基线 HEAD 不一致 → 源码比产物新 →
**无证据就不提示**），无基线但证据显示产物不旧时就地认账写基线（`origin="inferred"`）；
源码范围从两个 `src` 目录扩到构建真正读取的那些根（`config.SRC_ROOTS`：apps/packages/native/
vendor/scripts）+ 根级构建输入（`package.json`/`pnpm-lock.yaml`/`tsconfig*`/`tsdown.config.*`，
排除 `*.tsbuildinfo` 这类输出）；
launch 侧拒绝构建只告警、继续启动（产物真缺失时后面自有明确报错）。

**命令拼装事故（2026-09-24，DSH 0.1.7-rc.2）**：`_pnpm_command` 曾在字符串形式（真机走的
就是这条：`shutil.which("pnpm")` → `pnpm.CMD`）里自动补一个 `run`，而调用方传的是原样
argv（`"run", "build"`），真机于是执行 `pnpm run run build` → `[ERR_PNPM_NO_SCRIPT]
Missing script: run`，跨版本 `vdsh update dsh` 的清缓存与构建**全部失败**；列表形式
（离线复测注入 `[python, _fake_pnpm.py]`）不补 `run`，所以**复测 30+ 断言全绿、真机全红**。
现在两种形式同构（`args` 原样拼接，见 `_pnpm_command`），失败诊断另加 missing-script 指纹
（`missing_script`）：请求 `build`/`clean` 却报缺 `run` → 直说「命令拼装错误」且不再给清缓存
建议；报缺的恰是请求的脚本 → 提示 DSH 可能改了脚本名。`cleaned` 也改为记「已尝试清缓存」，
清缓存自身失败时不再把用户指回刚失败的那个动作。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

from .. import settings as settings_mod
from ..config import (
    BUILD_LOG_PATH,
    BUILD_PROMPT,
    BUILD_PROMPTS,
    BUILD_TAIL_LINES,
    CLI_REL,
    DIST_REL,
    EXIT_BUILD,
    EXIT_DEPS,
    EXIT_USAGE,
    SRC_ROOT_FILES,
    SRC_ROOT_PREFIXES,
    SRC_ROOTS,
    SRC_SKIP_DIRS,
    SRC_SKIP_SUFFIXES,
)
from ..console import die, ok, step, warn
from ..pnpm_log import Noise, has_network_failure
from ..spinner import EXIT_SPAWN_FAILED, run_child_progress

NAME = "build"
SUMMARY = "执行仓库构建（pnpm run build，带动画）"

# `build_reason()` 的两种「需构建」原因；同时是 config.BUILD_PROMPTS 的键。
REASON_MISSING = "missing"   # 产物文件不存在（铁证）
REASON_STALE = "stale"       # 证据显示产物比源码/提交旧

# 构建基线：成功构建后写入仓库 `lib/`（与产物同生共死——`pnpm run clean` 清掉它就等于
# 「未知基线」，下次启动照旧按证据判定）。内容：HEAD sha + 时间 + 版本 + 来源，供排查回溯。
STAMP_REL = os.path.join("lib", ".vdsh-build.json")
# 基线来源：`build` = 真实构建写下；`inferred` = 启动检测时按证据认账（见 `build_reason()`）。
STAMP_BUILD = "build"
STAMP_INFERRED = "inferred"

# 陈旧产物指纹：增量构建没重刷 lib/ 时，打包器报的是「导入的东西不存在」——这些指纹
# 与真实源码错误（TS 类型错误、语法错误）可区分，只有它们才触发清缓存重建。
# 样例（本机实测）：`[MISSING_EXPORT] "sanitizeProfile" is not exported by
# "../../packages/boot/app-boot/lib/index.js"`（rolldown/tsdown 文案）。
STALE_OUTPUT_RE = re.compile(
    r"MISSING_EXPORT|MISSING_IMPORT"
    r"|is not exported by"
    r"|Cannot find module '[^']*(?:lib|types)[/\\]"
    r'|Cannot find module "[^"]*(?:lib|types)[/\\]'
    r"|ERR_MODULE_NOT_FOUND"
    r"|Could not resolve [\"'][^\"']*(?:lib|types)[/\\]"
)


def looks_like_stale_output(lines, start=0):
    """构建输出是否命中「lib/ 产物与源码不同步」指纹（决定要不要清缓存重建）。

    start：只看从该下标起的行（重试判定只针对**第一次尝试**的输出，避免把重建阶段的
    真实报错误判成陈旧产物）。
    """
    for line in (lines or ())[start:]:
        if STALE_OUTPUT_RE.search(line):
            return True
    return False


# pnpm 报「没有这个脚本」的两种文案（本机 pnpm 11 实测：`[ERR_PNPM_NO_SCRIPT] Missing script: run`
# 与 `Command "run" not found.`）。抓出脚本名是为了把三类**完全不同**的原因分开：
# ① 报缺的是 vdsh 拼进去的子命令（`PNPM_RUN_VERB`）→ 命令拼装缺陷（2026-09-24 的
#    `pnpm run run build` 即此类）；② 报缺的正是本次请求的脚本 → 仓库改了脚本名；
# ③ 其余（子包/构建脚本内部的调用）→ 按仓库侧处理，不归咎启动器。
MISSING_SCRIPT_RE = re.compile(r"Missing script:\s*(\S+)|Command \"([^\"]+)\" not found")

# vdsh 跑仓库脚本用的 pnpm 子命令：`pnpm run <脚本>`。诊断据此判定「拼装把子命令当成了脚本名」
# ——`pnpm run run build` 时 pnpm 报缺的正是这个 `run`。**必须与调用点写的一致**，
# 由 `_check_build_cmd.py` 的 B 节（AST 扫描每个调用点的拼装结果）核对。
PNPM_RUN_VERB = "run"


def missing_script(lines):
    """输出里 pnpm 报「没有这个脚本」时返回脚本名，否则 None（出现多处时取最后一处）。"""
    found = None
    for line in lines or ():
        for match in MISSING_SCRIPT_RE.finditer(line):
            found = match.group(1) or match.group(2)
    return found


def head_commit(repo):
    """仓库 HEAD 的 commit sha；非 git 检出 / git 不可用时返回 None。

    复用 update 侧的 `_git_stdout`（只取 stdout）：git 的警告走 stderr，合并会污染判定。
    """
    from . import update as update_mod  # 延迟导入（update 在构建段反向延迟导入本模块）

    code, lines = update_mod._git_stdout(repo, "rev-parse", "HEAD")
    return lines[0] if code == 0 and lines else None


def _write_stamp(repo, origin=STAMP_BUILD):
    """成功构建后写基线；写不进去只告警（下轮会退化为证据判定，不影响结论）。"""
    path = os.path.join(repo, STAMP_REL)
    payload = {"head": head_commit(repo), "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
               "origin": origin}
    try:
        with open(os.path.join(repo, "package.json"), "r", encoding="utf-8") as handle:
            payload["version"] = json.load(handle).get("version", "?")
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return True
    except OSError as error:
        warn("构建基线未能写入 %s：%s" % (path, error))
        return False


def _read_stamp(repo):
    """读构建基线；文件在但内容损坏返回 `{}`（回退证据判定）；文件不存在返回 None。

    两种「读不到」必须分开：**缺失 = 基线未知**（从未经 vdsh 构建、`pnpm run clean` 清过、
    或用户自己 `pnpm run build` 的检出）→ 交给 `build_reason()` 按证据判定，不预设结论；
    **损坏 = 构建过但基线坏了** → 不该仅凭这条就每次都提示构建，退回 mtime 判定。
    """
    path = os.path.join(repo, STAMP_REL)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def newest_mtime(root, skip=(), skip_suffixes=()):
    """目录内所有文件的最大 mtime（Windows 上目录自身 mtime 不随内容编辑变化）。

    skip / skip_suffixes：整棵子树要跳过的目录名（产物/依赖：lib、dist、node_modules…）
    与要跳过的文件名后缀（构建输出：`*.tsbuildinfo`）。
    """
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        if skip:
            dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            if skip_suffixes and name.endswith(skip_suffixes):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    return newest


def source_newest_mtime(repo):
    """仓库源码的最新 mtime（`SRC_ROOTS` + 根级构建输入）；没有源码返回 0。

    `SRC_ROOTS` 是构建真正读取的那些根（apps/packages/native/vendor/scripts），
    排除产物与依赖目录，否则「产物比源码新」永远成立；根级再补 `package.json`、
    `pnpm-lock.yaml`、`tsconfig*`、`tsdown.config.*` 这些不在根目录树里的构建输入
    （注意排除同为 `tsconfig*` 的 `*.tsbuildinfo`——那是 `tsc -b` 的输出，比产物还新）。
    实测 7720 个文件约 0.5s（NTFS 热缓存），相对启动本身的秒级开销可忽略。
    """
    newest = 0.0
    for rel in SRC_ROOTS:
        root = os.path.join(repo, rel)
        if os.path.isdir(root):
            newest = max(newest, newest_mtime(root, SRC_SKIP_DIRS, SRC_SKIP_SUFFIXES))
    try:
        names = os.listdir(repo)
    except OSError:
        return newest
    for name in names:
        if not (name in SRC_ROOT_FILES or name.startswith(SRC_ROOT_PREFIXES)):
            continue
        if name.endswith(SRC_SKIP_SUFFIXES):
            continue
        path = os.path.join(repo, name)
        if not os.path.isfile(path):
            continue
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def build_reason(repo):
    """需要构建的原因：`REASON_MISSING` / `REASON_STALE`；无需构建返回 None。

    判定按证据强度递进，**没有任何证据就不提示**：
      1. 产物文件不存在 → `REASON_MISSING`；
      2. 构建基线的 HEAD ≠ 当前 HEAD（`git pull`/切分支/reset）→ `REASON_STALE`；
      3. 源码最新 mtime 新于产物 → `REASON_STALE`；
      4. 都没有 → None（并见下：无基线时顺手认账）。

    **「有产物、无基线」不再等于需构建**（2026-09-23 修正）：那正是「用户自己跑过
    `pnpm run build`/刚重下重建」的检出，改前一律提示构建，用户答 n 还会连启动一起取消
    （实测：没动过 DSH 的检出被问「源码更新」，答 n 直接 `已取消`，只能手动 `pnpm dsh web`）。
    现在这种检出按上面 2/3 两条查证据：证据说产物不旧就地**认账**写基线
    （`origin="inferred"`，与真实构建写下的基线区分开），下次启动只比 HEAD。
    """
    cli_bin = os.path.join(repo, CLI_REL)
    dist = os.path.join(repo, DIST_REL)
    if not os.path.isfile(cli_bin) or not os.path.isfile(dist):
        return REASON_MISSING
    stamp = _read_stamp(repo)
    if stamp is not None:
        recorded = stamp.get("head")
        # `recorded` 为空视为「基线不可用」（损坏）：不能拿 None 去比 HEAD——
        # 那样损坏一次就会每次启动都提示构建。缺 head 时直接落到 mtime 判定。
        if recorded:
            head = head_commit(repo)
            if head is not None and recorded != head:
                return REASON_STALE
    # 产物时间取两个产物里**较早**的那个：任一产物偏旧都算陈旧。
    built_at = min(os.path.getmtime(cli_bin), os.path.getmtime(dist))
    if source_newest_mtime(repo) > built_at:
        return REASON_STALE
    if stamp is None:
        _write_stamp(repo, origin=STAMP_INFERRED)
    return None


def build_needed(repo):
    """是否需要构建（`build_reason()` 的布尔形式，语义见其 docstring）。

    注意：为「有产物、无基线」的检出写基线（`origin="inferred"`）是本函数的一处有意副作用，
    只写 `lib/.vdsh-build.json`（构建产物目录内），不写别的状态。
    """
    return build_reason(repo) is not None


def confirm_build(reason=None):
    """构建确认；reason 决定提示文案（`REASON_MISSING` / `REASON_STALE`）。

    拒绝的三种形态（答 n / 非交互 EOF / Ctrl+C）都返回 False，语义统一为「跳过构建」——
    调用方据此继续启动，而不是取消整个命令（见 launch.run 的注释）。
    """
    prompt = BUILD_PROMPTS.get(reason, BUILD_PROMPT)
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        # 非交互环境（stdin 重定向）：默认不构建，继续按现有产物启动。
        return False
    except KeyboardInterrupt:
        # 提示符处 Ctrl+C = 「别问了」：跳过构建继续启动（真想中断启动再按一次）。
        print("", file=sys.stderr)
        return False
    return answer in ("", "y", "yes")


def _write_build_log(lines):
    """把构建输出全文落盘（失败时可回溯）；写不进去只告警，不影响构建结论。"""
    try:
        with open(BUILD_LOG_PATH, "w", encoding="utf-8", errors="replace") as handle:
            for line in lines:
                handle.write(line + "\n")
        return True
    except OSError as error:
        warn("构建日志未能写入 %s：%s" % (BUILD_LOG_PATH, error))
        return False


def _prune_build_log():
    """构建成功后删除日志：只保留最近一次失败现场，避免 %TEMP% 堆积。"""
    try:
        os.remove(BUILD_LOG_PATH)
    except OSError:
        pass


def clean_build(repo, pnpm, lines=None):
    """执行 `pnpm run clean`（删除各包 lib/ 与 tsbuildinfo）；清不掉只告警不中断。

    `tsx scripts/clean.ts` 按仓库项目引用图删除构建输出（`lib/`、`*.tsbuildinfo`），
    不触碰 `apps/web/dist`、`node_modules` 与工作区数据（2026-09-23 静态核对图 +
    脚本源码确认）；脚本自身也会拒绝越界/含未知文件的目录。
    """
    step("清理构建产物（各包 lib/ 与 tsbuildinfo）")
    collected = [] if lines is None else lines
    code = run_child_progress(_pnpm_command(pnpm, "run", "clean"), "清理构建产物", cwd=repo,
                              collect=collected, quiet=Noise())
    if code != 0:
        warn("清缓存未完成（exit code %d）：继续按普通方式重建，失败诊断里会说明这一点" % code)
        return False
    return True


def _pnpm_command(pnpm, *args):
    """拼一条 pnpm 命令：`<pnpm 可执行文件> <args…>`，`args` **原样**接在后面。

    pnpm 允许是路径（正常：`shutil.which("pnpm")` 给的可执行文件）或 argv 前缀列表
    （离线复测注入 `[sys.executable, _fake_pnpm.py]`）。两种形式**同构**：调用方写下的
    就是子进程真正收到的——`_pnpm_command(pnpm, "run", "build")` → `pnpm run build`。

    **2026-09-24 事故（不要复活自动补 `run`）**：本函数曾在字符串形式里补一个 `run`，
    而调用方按「原样 argv」传 `("run", "build")`，真机于是执行 `pnpm run run build` →
    `[ERR_PNPM_NO_SCRIPT] Missing script: run`，`vdsh update dsh` 的跨版本构建全部失败。
    列表形式（离线复测唯一覆盖的形式）不补 `run`，所以**复测全绿、真机全红**：
    「两种形式行为不同」本身就是缺陷，现由 `_check_build_cmd.py` 固化守卫。
    """
    if isinstance(pnpm, str):
        return [pnpm, *args]
    return [*pnpm, *args]


def run_build(repo, pnpm=None, code_is_new=False, clean=False, retry_on_stale=True):
    """执行 pnpm run build；失败时给出 exit code + 末尾输出 + 完整日志路径。

    pnpm：可选的 pnpm 可执行文件路径（默认按 PATH 解析；测试用注入 stub）。
    code_is_new：调用方是否刚把检出的代码更新到最新（`vdsh update dsh` 传 True）——
    只影响失败收尾那句「代码已更新、仅构建失败」是否成立。
    clean：先 `pnpm run clean` 再构建（全量重建，`vdsh build --clean` 与
    `update dsh` 的版本变更路径用），此路径**不再二次重试**。
    retry_on_stale：增量构建失败且命中陈旧产物指纹时，清缓存后重建一次
    （默认 True；`--no-retry` 关掉）。

    成功 → 写构建基线（`STAMP_REL`）供启动侧时效判定；失败 → 全程输出落盘 + 末尾复述。
    两次尝试的输出累积在同一个 lines 里一并落盘（失败现场只有一份日志）。
    """
    if pnpm is None:
        pnpm = shutil.which("pnpm")
    if pnpm is None:
        # 退出码与 update 侧一致：pnpm 缺失是环境依赖问题（EXIT_DEPS），不是构建失败。
        die("未找到 pnpm（请安装 pnpm 并加入 PATH）", EXIT_DEPS)
    noise = Noise()
    lines = []
    started = time.monotonic()
    # 失败时**只看末尾几行**（构建工具的真报错都在尾部、且一定没被折叠）：
    # 用 collect 收全量、自己只打末尾，避免「折叠行补打 + tail 复述」把同一批行打两遍。
    # `cleaned` 记的是「**已尝试**清缓存」，不是「清成功」：`clean_build` 自己失败时也必须这么记，
    # 否则诊断会把用户指回刚刚失败的那个动作（2026-09-24 实测：版本变更走 `--clean`，清缓存失败
    # 被记成「没清过」，于是打出与现场相反的「可先清缓存再重建」，掩盖了真实根因）。
    cleaned = False
    clean_ok = False
    if clean:
        cleaned = True
        clean_ok = clean_build(repo, pnpm, lines)
    first_attempt_start = len(lines)
    code = _run_build_once(repo, pnpm, lines, noise)
    spawn_failed = code == EXIT_SPAWN_FAILED
    if not clean and code != 0 and retry_on_stale and looks_like_stale_output(lines, first_attempt_start):
        warn("构建失败，疑似 lib/ 产物与源码不同步（增量构建未重刷产物）")
        step("清缓存后自动重建一次（全量，比增量更久）")
        # 进入这条分支即「已尝试清缓存」：即便这次 clean 自己失败（`clean_build` 只告警），
        # 诊断也不必再把用户指回同一个动作。
        clean_ok = clean_build(repo, pnpm, lines)
        cleaned = True
        code = _run_build_once(repo, pnpm, lines, noise)
        spawn_failed = spawn_failed and code == EXIT_SPAWN_FAILED
    duration = time.monotonic() - started
    # 两次都起不来才算 pnpm 环境问题（起得来但退非 0 是构建失败，交给下面统一诊断）。
    if spawn_failed:
        die("无法启动 pnpm（%s）：请确认可执行且未被安全软件拦截" % pnpm, EXIT_DEPS)
    if code != 0:
        _report_build_failure(repo, code, lines, code_is_new, cleaned=cleaned, clean_ok=clean_ok,
                              scripts=("clean", "build") if cleaned else ("build",))
        die("构建失败（exit code %d）" % code, EXIT_BUILD)
    _write_stamp(repo)
    _prune_build_log()
    ok("构建完成")
    step("用时 %.1fs" % duration)


def _run_build_once(repo, pnpm, lines, noise):
    """跑一次 `pnpm run build`，把输出追加进 lines，返回退出码。"""
    return run_child_progress(_pnpm_command(pnpm, "run", "build"), "构建", cwd=repo,
                              quiet=noise, collect=lines)


def _report_build_failure(repo, code, lines, code_is_new, cleaned=False, clean_ok=False, scripts=()):
    """失败诊断（不受输出简约约束）：末尾输出 + 完整日志 + 仓库状态说明。

    code_is_new：调用方是否刚把检出的代码更新到最新（update dsh 的构建段）——
    决定收尾那句是「代码已更新、仅构建失败」还是纯「构建失败」。
    cleaned / clean_ok：是否已尝试清缓存 / 清缓存是否真的成功——决定下一步给什么
    （已清过就别再让用户清一次；没清过时给可执行的 `pnpm run clean && pnpm run build`，
    而不是改前那条已证明无效的 `pnpm install && pnpm run build`）。
    scripts：本次**打算跑**的脚本名（正常 `("build",)`，`--clean` 路径 `("clean", "build")`）。
    与输出里 pnpm 报缺的脚本名对照，把「命令拼装错」与「仓库没有这个脚本」分开——
    两类的下一步完全相反，混在一起就会给出与现场相反的建议（见 `missing_script`）。
    """
    print("", file=sys.stderr)
    warn("构建输出末尾 %d 行（完整日志见下）：" % min(BUILD_TAIL_LINES, len(lines)))
    for line in lines[-BUILD_TAIL_LINES:]:
        print(line, file=sys.stderr)
    if _write_build_log(lines):
        warn("完整构建日志：%s" % BUILD_LOG_PATH)
    if has_network_failure(lines):
        warn("构建失败（exit code %d），疑似网络不可达（可配置 HTTPS_PROXY 或稍后重试）" % code)
    else:
        warn("构建失败（exit code %d）：上面末尾输出即根因线索" % code)
    if code_is_new:
        warn("仓库代码已更新到最新，仅构建未完成")
    missing = missing_script(lines)
    # 只有「报缺的正是 vdsh 拼进去的子命令」才算拼装错——这样既拦得住 2026-09-24 那类缺陷，
    # 也不会把子包脚本缺失（`tsx scripts/build.ts` 内部的 pnpm 调用）误判成启动器的问题。
    invocation_defect = missing == PNPM_RUN_VERB and missing not in scripts
    if invocation_defect:
        warn("vdsh 请求的是 %s，pnpm 却报没有 `%s` 脚本：**命令拼装错误**（把子命令当成了脚本名，"
             "启动器缺陷、与仓库无关）——清缓存/重试都无效，请带上这行反馈"
             % ("、".join("`%s`" % name for name in scripts) or "（未知脚本）", missing))
    elif missing:
        warn("仓库没有 `%s` 脚本（pnpm: Missing script）：请核对 %s 的 package.json scripts"
             "（若该调用来自子包或构建脚本内部，则与 vdsh 无关）" % (missing, repo))
    elif cleaned and clean_ok:
        warn("已清理构建产物并重建仍未通过：属真实构建失败（不是陈旧产物），请按上面报错处理")
    elif cleaned:
        warn("清缓存未能执行、重建仍未通过：属真实构建失败；请确认 pnpm/tsx 可用后重试")
    else:
        warn("可先清缓存再重建（陈旧产物导致的缺导出/找不到模块，重跑普通构建无效）："
             "在 %s 执行 `pnpm run clean && pnpm run build`" % repo)
    if not invocation_defect:
        warn("重建前请确认 dsh web 已停止（文件锁会让构建写不进产物）")


def run(argv, settings):
    """独立命令：vdsh build [--clean] [--no-retry]（直接构建，不经确认）。"""
    clean = False
    retry_on_stale = True
    for token in argv:
        if token == "--clean":
            clean = True
        elif token == "--no-retry":
            retry_on_stale = False
        else:
            die("未知参数: %s（用法: vdsh build [--clean] [--no-retry]）" % token, EXIT_USAGE)
    repo = settings_mod.effective_repo(settings)
    if not os.path.isfile(os.path.join(repo, "package.json")):
        die("未找到 Harness 仓库（%s），请设置 DSH_REPO 或编辑 vdsh.yaml 的 launcher.repo" % repo,
            EXIT_USAGE)
    if clean:
        step("全量重建（--clean：先清 lib/ 与 tsbuildinfo，耗时更长）")
    run_build(repo, clean=clean, retry_on_stale=retry_on_stale)
    return 0
