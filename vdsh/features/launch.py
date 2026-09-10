# -*- coding: utf-8 -*-
"""启动功能（默认命令）：一键启动 dsh web 并在就绪后打开浏览器。

插件做不到的事：进程/仓库管理、端口探测与就绪轮询、工作区 RPC 注册、
Tailscale 信任围栏与窗口最小化，都发生在 DSH 进程之外，
因此本功能在 launcher 层实现，与 Web UI 插件互不干扰。
"""

import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .. import settings as settings_mod
from ..config import (
    API_RPC_URL,
    AUTH_REQUIRED_MARKER,
    BOOT_MARKER,
    CLI_REL,
    EXIT_BUILD,
    EXIT_DEPS,
    EXIT_NO_PWSH,
    EXIT_PORT_BUSY,
    EXIT_USAGE,
    POLL_TIMEOUT,
    PORT,
    SEED_MJS,
    SEED_YML,
    STARTING_WAIT_GAP,
    TCP_TIMEOUT,
    TITLE_MARKER,
    URL,
    WEB_URL_LOG,
    WORKSPACE_CREATE_ENDPOINT,
)
from ..console import die, ok, step, warn
from ..module_fallback import heal_module_fallback
from ..spinner import Spinner

NAME = "launch"
SUMMARY = "启动 dsh web 并打开浏览器（默认命令）"

# 启动器拉起的 dsh web 进程在就绪前退出（崩溃/启动失败）时抛出；消息已格式化。
class ServerExitedError(Exception):
    pass


# ── 启动参数（原 cli.parse_args 的 launch 分支）────────────────────────────
def parse_launch_args(argv):
    """解析启动参数：vdsh [--tailnet <域名>] [--sync] [工作目录]。

    返回 {'workspace', 'tailnet', 'auto_pull'}；非法输入 die(EXIT_USAGE)。
    """
    tailnet = None
    auto_pull = False
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--tailnet":
            index += 1
            if index >= len(argv):
                die("--tailnet 需要域名值（如 --tailnet xxx.ts.net）", EXIT_USAGE)
            tailnet = argv[index]
        elif arg == "--sync":
            auto_pull = True
        elif arg.startswith("-"):
            die("未知参数: %s" % arg, EXIT_USAGE)
        else:
            positional.append(arg)
        index += 1
    tailnet = settings_mod.normalize_tailnet(tailnet)
    if len(positional) > 1:
        die("最多接受 1 个工作目录参数，收到 %d 个" % len(positional), EXIT_USAGE)
    workspace = positional[0] if positional else os.getcwd()
    if not os.path.isdir(workspace):
        die("工作目录不存在: %s" % workspace, EXIT_USAGE)
    return {
        "workspace": os.path.abspath(workspace),
        "tailnet": tailnet,
        "auto_pull": auto_pull,
    }


# ── 服务探测 / 启动 / 就绪轮询 ─────────────────────────────────────────────
def http_get(url):
    """本机探测必须绕过环境代理：HTTP_PROXY/HTTPS_PROXY 会把 loopback 请求转发到代理而超时。"""
    return requests.get(url, timeout=POLL_TIMEOUT, proxies={"http": None, "https": None})


def is_harness_page(resp):
    """判定响应是否为就绪的 Harness 页面。

    主标记为 window.__DSH_BOOT__：dsh web 服务端注入的引导清单（品牌无关，
    0.1.1-rc.2 起标题品牌化后依然稳定存在）；旧标题仅作兼容回退。
    """
    return BOOT_MARKER in resp.text or TITLE_MARKER in resp.text


# ── 浏览器会话认证（DSH 0.1.3-alpha.1+）──────────────────────────────────────
# 新 DSH 对根页面与 /api 实施认证：GET /?token=<per-process launch token> →
# 303 → 干净 / + Set-Cookie（HttpOnly）；无 token/cookie → 401。
# 服务器在 Loader 结算后向 stdout 打印 `dsh web: <认证URL>`（--no-open 也打印）
# 作为就绪信号；启动器把该行捕获到 WEB_URL_LOG 才能完成认证与就绪判定。

def _url_line_from_log(log_path):
    """从启动日志提取认证 URL（`dsh web: <url>`，含 ?token=）；未就绪返回 None。"""
    try:
        path = Path(log_path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*dsh web:\s*(\S+)", text)
    return match.group(1) if match else None


def _lan_url_from_log(log_path):
    """提取日志行中的 LAN 认证 URL（`(LAN: <url>)`），供手机端访问；无则 None。"""
    try:
        path = Path(log_path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"\(LAN:\s*(\S+)\)", text)
    return match.group(1) if match else None


def _token_of(url):
    """从认证 URL 提取 token 查询参数；无则 None。"""
    if not url:
        return None
    try:
        return parse_qs(urlsplit(url).query).get("token", [None])[0]
    except ValueError:
        return None


def _auth_session_bootstrap(auth_url):
    """用认证 URL 完成 token→cookie 交换，返回带 cookie 的 Session；失败返回 None。

    经 GET /?token=… → 303 → / 后 requests 自动带 cookie 跟到页面；
    旧 DSH 忽略查询参数直接返回页面，同样返回可用 Session（cookie 为空，无害）。
    """
    try:
        session = requests.Session()
        resp = session.get(auth_url, timeout=POLL_TIMEOUT, proxies={"http": None, "https": None})
        if resp.status_code == 200 and is_harness_page(resp):
            return session
    except requests.RequestException:
        pass
    return None


def probe_harness():
    """探测运行状态：'idle' 无监听；'ready' 200+引导清单；'auth' 新 DSH 已在运行
    （浏览器会话认证返回 401 正文）；'starting' 端口占用但页面未就绪。"""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=TCP_TIMEOUT):
            pass
    except OSError:
        return "idle"
    try:
        resp = http_get(URL)
        if resp.status_code == 200 and is_harness_page(resp):
            return "ready"
        if resp.status_code == 401 and AUTH_REQUIRED_MARKER in resp.text:
            return "auth"
    except requests.RequestException:
        pass
    return "starting"


def _http_ready():
    """旧式 HTTP 就绪判定：200 + 引导清单（无认证的旧 DSH）。"""
    try:
        resp = http_get(URL)
        return resp.status_code == 200 and is_harness_page(resp)
    except requests.RequestException:
        return False


def _log_tail(log_path, max_lines=20):
    """日志末段（最近 max_lines 行，去尾部空行）；读不到返回 None。"""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace").rstrip("\r\n")
    except OSError:
        return None
    lines = text.splitlines()[-max_lines:]
    return "\n".join(lines)


def wait_until_ready(deadline, gap, spinner_message=None, on_ready=None, log_path=None, proc=None):
    """轮询直到 Harness 就绪；返回就绪信息 dict（或 None=超时）。

    ready = {'authed_url': str|None, 'session': Session|None, 'lan_url': str|None}
    log_path 提供时优先等 DSH 进程打印的 `dsh web: <认证URL>` 行——0.1.3-alpha.1
    起该行是平台自身的就绪信号（Loader 结算后才打印），拿到后再经 token→cookie
    交换校验页面；无 URL 行回退旧式 HTTP 探测（兼容无认证的旧 DSH）。
    就绪后先停止动画，再调用 on_ready(ready)。
    """
    spinner = Spinner(spinner_message)
    spinner.start()
    ready = None
    waited = 0.0
    try:
        while deadline is None or waited < deadline:
            if log_path is not None:
                auth_url = _url_line_from_log(log_path)
                if auth_url is not None:
                    session = _auth_session_bootstrap(auth_url)
                    if session is not None:
                        ready = {"authed_url": auth_url, "session": session,
                                 "lan_url": _lan_url_from_log(log_path)}
                        break
                elif _http_ready():
                    ready = {"authed_url": None, "session": None, "lan_url": None}
                    break
            elif _http_ready():
                ready = {"authed_url": None, "session": None, "lan_url": None}
                break
            # 就绪前子进程已退出（典型：CLI 产物缺失/启动崩溃）→ 立即失败并给日志尾部，
            # 不再无反馈地等待到超时。
            if proc is not None and proc.poll() is not None:
                tail = _log_tail(log_path) if log_path else None
                detail = ("；日志尾部:\n%s" % tail) if tail else ""
                raise ServerExitedError(
                    "dsh web 进程已退出（退出码 %s），未完成启动%s" % (proc.returncode, detail))
            time.sleep(gap)
            waited += gap
    finally:
        spinner.finish()
    if ready is not None and on_ready is not None:
        on_ready(ready)
    return ready


def _body_snippet(resp, limit=160):
    """响应正文单行摘要（截断）——协议漂移时替代难懂的 JSON 解析异常。"""
    return " ".join(resp.text.split())[:limit] or "（空响应）"


def register_workspace(path, session=None):
    """向已在运行的 Harness 实例注册工作区（POST /api/workspace/create，幂等）。

    Connection RPC 契约（0.1.3-alpha.1，见 config.py 的 WORKSPACE_CREATE_ENDPOINT）：
    端点路径为 /api/<namespace>/<method>，报文为 payload.args.request；
    点号端点与裸 payload 都会得到 404 纯文本（曾表现为「Expecting value: line 1
    column 1 (char 0)」这类无信息量的告警）。
    /api 另需浏览器会话 cookie：传入已认证的 session（从启动日志的认证 URL 交换
    得到）才能通过；无 cookie 时后端返回 401。
    返回 True 表示注册成功；任何失败仅告警，不阻断打开浏览器。
    """
    endpoint = WORKSPACE_CREATE_ENDPOINT
    envelope = {
        "type": "client-request",
        "rpcId": uuid.uuid4().hex,
        "method": endpoint,
        "payload": {"args": {"request": {"path": path}}},
    }
    client = session if session is not None else requests
    try:
        resp = client.post(
            "%s/%s" % (API_RPC_URL, endpoint),
            json=envelope,
            timeout=POLL_TIMEOUT,
            proxies={"http": None, "https": None},
        )
    except requests.RequestException as error:
        warn("工作区注册失败（%s）" % error)
        return False
    if resp.status_code == 401:
        warn("工作区注册需要浏览器认证（先用 DSH 控制台打印的 URL 打开一次页面）")
        return False
    try:
        result = resp.json()["result"]
    except (ValueError, KeyError, TypeError):
        warn("工作区注册失败（HTTP %d：%s）" % (resp.status_code, _body_snippet(resp)))
        return False
    if isinstance(result, dict) and result.get("ok") is True:
        return True
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        warn("工作区注册被拒绝（%s: %s）" % (error.get("code", "unknown"), error.get("message", "")))
    else:
        warn("工作区注册失败（HTTP %d：%s）" % (resp.status_code, _body_snippet(resp)))
    return False


def tailnet_is_trusted(tailnet):
    """运行中的实例是否信任该域名（Host 围栏）：403=未信任，其余=已信任，None=无法判定。

    用请求头 Host 冒充该域名打 /api：围栏先于认证与端点分发生效，故无需 cookie
    （已信任 → 401/404，未信任 → 403，实测见 doc/experience.md §7.3）。
    探测用「传给 --trusted-host 的同一个字符串」，与启动参数同源。
    """
    try:
        resp = requests.get(
            "%s/%s" % (API_RPC_URL, WORKSPACE_CREATE_ENDPOINT),
            headers={"Host": tailnet},
            timeout=POLL_TIMEOUT,
            proxies={"http": None, "https": None},
        )
    except requests.RequestException:
        return None
    return resp.status_code != 403


def open_url(url):
    os.startfile(url)


def spawn_server(repo, workspace, patch_path, tailnet):
    """弹独立最小化 pwsh 窗口启动 dsh web；stdout/stderr 全流重定向到 WEB_URL_LOG。

    0.1.3-alpha.1 起 dsh web 把「认证 URL 行」（dsh web: http://…/?token=…）打印到
    stdout 作为就绪信号；重定向后 vdsh 才能捕获它完成浏览器认证与就绪判定，
    DSH 控制台输出也落在该日志（排障时查看）。
    不带 -NoExit：node 退出（含启动崩溃）后窗口自动关闭，launcher 经返回的
    进程句柄立即感知失败并反馈，而不是空转到超时。
    返回已启动的 Popen 句柄（供 wait_until_ready 检测提前退出）。
    """
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        die("未找到 pwsh（PowerShell 7），请安装并加入 PATH", EXIT_NO_PWSH)
    if shutil.which("node") is None:
        die("未找到 node（Node.js），请安装并加入 PATH", EXIT_DEPS)
    cli = os.path.join(repo, CLI_REL)
    try:
        if WEB_URL_LOG.exists():
            WEB_URL_LOG.unlink()
    except OSError:
        pass  # 清不掉也继续：pwsh 重定向会覆盖
    # CREATE_NEW_CONSOLE 弹独立窗口；SW_SHOWMINNOACTIVE 最小化且不抢占焦点。
    # node 路径加引号包裹，兼容含空格的仓库路径。
    # --patch 是 launcher 层选项，必须位于任何 app 参数之前
    # （enablePositionalOptions 会把首个位置参数之后的选项透传给 app）。
    # --no-open：0.1.1 起 web app 默认自行打开浏览器（会与 launcher 就绪后的
    # 打开动作重复），故关闭服务端自动打开，只由 launcher 在就绪后打开一次。
    # 注意 --trusted-host 是可变参数，--no-open 必须在其后。
    command = 'node "%s" web' % cli
    if patch_path is not None:
        command += ' --patch "%s"' % patch_path
    if tailnet is not None:
        command += ' --trusted-host "%s"' % tailnet
    command += ' --no-open'
    # PowerShell 全流重定向到日志（捕获认证 URL 行；窗口内不再回显）。
    command += ' *> "%s"' % WEB_URL_LOG
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    try:
        proc = subprocess.Popen(
            [pwsh, "-WorkingDirectory", workspace, "-Command", command],
            cwd=workspace,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
    except OSError as error:
        die("无法弹出服务窗口（%s）" % error, EXIT_NO_PWSH)
    return proc


# ── 工作区种子（用户侧插件，经 --patch 注入）──────────────────────────────
WORKSPACE_SEED_MJS = """\
// workspace-seed.mjs — 启动时把宿主进程 cwd 幂等注册为 Web UI 工作区。
// 用户侧插件（非 Harness 仓库源码），经 `dsh web --patch <seed.yml>` 注入。
// apply 必须 await create：注册需在 loader 结算（URL 行打印）之前完成，
// 否则客户端首个 workspace baseline 可能先于注册到达，startInitialSelection
// 只执行一次，会错过 A（表现为侧边栏出现 A 但未自动选中）。
export const name = 'workspace-seed'
export const inject = ['workspaceRegistry']

export async function apply(ctx, config) {
  try {
    await ctx.workspaceRegistry.create(config.path)
  } catch (error) {
    ctx.logger.warn(`workspace-seed: failed to register '${config.path}': ${String(error)}`)
  }
}
"""


def ensure_seed_patch():
    """确保种子插件存在并生成 seed.yml；失败返回 None（仅警告，不阻断启动）。"""
    try:
        if not SEED_MJS.exists():
            SEED_MJS.write_text(WORKSPACE_SEED_MJS, encoding="utf-8")
        content = (
            "# workspace-seed patch：把启动目录幂等注册进工作区注册表（由 vdsh_launcher 生成）。\n"
            "- insert:\n"
            "    - id: workspace-seed\n"
            "      name: %s\n"
            "      inject: [workspaceRegistry]\n"
            "      config:\n"
            "        path: !!js process.cwd()\n" % SEED_MJS.as_uri()
        )
        SEED_YML.write_text(content, encoding="utf-8")
        return str(SEED_YML)
    except OSError as error:
        warn("无法写入种子文件（%s），本次启动不自动注册工作区。" % error)
        return None


# ── 主流程 ────────────────────────────────────────────────────────────────
def _open_existing(workspace_path, tailnet, want_browser, ready=None):
    session = (ready or {}).get("session")
    if session is not None:
        register_workspace(workspace_path, session=session)
    else:
        register_workspace(workspace_path)  # 尽力而为（新 DSH 无 cookie 会 401 并提示）
    authed_url = (ready or {}).get("authed_url")
    if want_browser:
        open_url(authed_url or URL)
    ok("dsh web 已在运行 → %s" % URL)
    # 只在围栏明确拒绝（403）时告警：实例可能本就带 --trusted-host（如 tailnet
    # 来自 vdsh.yaml），无条件告警是假警报；探测失败/未知则保持安静。
    if tailnet is not None and tailnet_is_trusted(tailnet) is False:
        warn("运行中的实例未带 --trusted-host，手机访问会 403；"
             "请关闭后重启（vdsh --tailnet %s）" % tailnet)


def _open_launched(tailnet, want_browser, ready, started=None):
    authed_url = ready.get("authed_url")
    if want_browser:
        open_url(authed_url or URL)  # 打开带 token 的认证 URL（浏览器首访换 cookie）
    ok("dsh web 就绪 → %s" % URL)
    if started is not None:
        step("用时 %.1fs" % (time.monotonic() - started))
    if tailnet is not None:
        token = _token_of(authed_url)
        if token:
            step("手机访问 → https://%s/?token=%s" % (tailnet, token))
        else:
            step("手机访问 → https://%s/" % tailnet)
    else:
        lan_url = ready.get("lan_url")
        if lan_url:
            step("手机访问 → %s" % lan_url)


def run(argv, settings):
    """启动功能入口：ready/starting/idle 状态机（默认命令）。"""
    from . import build as build_feature, sync as sync_feature

    payload = parse_launch_args(argv)
    repo = settings_mod.effective_repo(settings)
    if not os.path.isfile(os.path.join(repo, "package.json")):
        # 跨机复制 launcher 时 vdsh.yaml 残留另一台机器的仓库路径：探测本机候选并自愈；
        # DSH_REPO 显式设置时尊重环境变量，不覆盖。
        probed = settings_mod.probe_repo()
        if probed and not os.environ.get("DSH_REPO"):
            warn("配置的仓库不存在（%s），已改用本机仓库 %s 并写回 vdsh.yaml" % (repo, probed))
            ok_write, _config_warnings = settings_mod.patch_values(
                {("launcher", "repo"): probed})
            if not ok_write:
                warn("vdsh.yaml 写入失败，本次直接使用探测到的仓库")
            repo = probed
        else:
            die("未找到 Harness 仓库（%s），请设置 DSH_REPO 或编辑 vdsh.yaml 的 launcher.repo"
                "（可运行 vdsh setup 重新配置）" % repo, EXIT_USAGE)

    workspace_path = payload["workspace"]
    tailnet = settings_mod.effective_tailnet(payload["tailnet"], settings)
    launcher_cfg = settings["launcher"]
    # 启动前自动拉取：CLI --sync 或 launcher.auto_pull（配置项）命中即开启。
    auto_pull = payload["auto_pull"] or launcher_cfg["auto_pull"]

    status = probe_harness()
    if status in ("ready", "auth"):
        # 已在运行：把当前工作目录注册进该实例（幂等，失败已内部告警），然后打开浏览器。
        # 数据同步只在全新启动前做，实例运行中数据正被写入，跳过。
        if auto_pull:
            step("实例已在运行，跳过自动同步（需要时用 vdsh sync pull）")
        # 新 DSH（auth）且是启动器启动的实例：启动日志里仍是当前进程的认证 URL，
        # 可完成 cookie 交换后开浏览器并注册工作区；否则退回无认证路径。
        ready = None
        if status == "auth":
            auth_url = _url_line_from_log(WEB_URL_LOG)
            session = _auth_session_bootstrap(auth_url) if auth_url else None
            if session is not None:
                ready = {"authed_url": auth_url, "session": session,
                         "lan_url": _lan_url_from_log(WEB_URL_LOG)}
            else:
                warn("运行中的实例启用了浏览器认证：请用 DSH 控制台打印的 URL 打开（日志 %s）"
                     % WEB_URL_LOG)
        _open_existing(workspace_path, tailnet, launcher_cfg["open_browser"], ready)
        return 0
    if status == "starting":
        # 端口已占用但页面未就绪：可能是上次启动尚未完成（插件多时启动慢），
        # 等待其就绪而不是重复拉起第二个实例。
        if auto_pull:
            step("实例启动中，跳过自动同步（需要时用 vdsh sync pull）")
        found = wait_until_ready(
            deadline=launcher_cfg["starting_budget_seconds"],
            gap=STARTING_WAIT_GAP,
            spinner_message="等待实例就绪",
            log_path=WEB_URL_LOG,
            on_ready=lambda r: _open_existing(workspace_path, tailnet, launcher_cfg["open_browser"], r),
        )
        if found:
            return 0
        die("端口 %d 已被占用但未识别为 Harness，请检查后重试" % PORT, EXIT_PORT_BUSY)

    # 全新启动：先做「开工前拉取」（对应日常规则），任何结果都不阻断启动流程。
    if auto_pull:
        sync_feature.auto_sync_pull(settings)

    if build_feature.build_needed(repo):
        if not build_feature.confirm_build():
            step("已取消", to_stderr=True)
            return 0
        build_feature.run_build(repo)

    # 启动前预检：CLI 产物必须存在（缺失时 node 立即崩溃，此前只会空转到超时）。
    cli_bin = os.path.join(repo, CLI_REL)
    if not os.path.isfile(cli_bin):
        die("未找到 dsh CLI 产物（%s）：请先执行 vdsh build；若仓库路径不对，"
            "运行 vdsh setup 或设置 DSH_REPO 后重试。" % cli_bin, EXIT_BUILD)

    # 模块回退自愈：git 同步在 Windows 上把 junction 展开成真实目录/文件入库，
    # 副机检出后 dsh 启动会报「exists and is not a symlink or dsh-managed module proxy」。
    # 这里按 app-boot 规则清理非链接、非 proxy 条目（仅污染数据，dsh 会自动重建）；
    # 只处理数据目录存在的情形，失败不阻断启动（dsh 的报错会给出明确提示）。
    data_dir = settings_mod.effective_data_dir(settings) or os.path.expanduser("~/.dsh")
    healed = heal_module_fallback(data_dir)
    if healed > 0:
        warn("已清理 %d 个同步污染的回退条目（dsh 启动时会自动重建）" % healed)

    patch_path = ensure_seed_patch() if launcher_cfg["workspace_seed"] else None
    started = time.monotonic()
    proc = spawn_server(repo, workspace_path, patch_path, tailnet)
    step("启动 dsh web（工作区 %s）" % workspace_path)
    try:
        ready = wait_until_ready(
            deadline=launcher_cfg["startup_timeout_seconds"],
            gap=launcher_cfg["poll_gap_seconds"],
            spinner_message="启动 dsh web",
            log_path=WEB_URL_LOG,
            proc=proc,
            on_ready=lambda r: _open_launched(tailnet, launcher_cfg["open_browser"], r, started),
        )
    except ServerExitedError as error:
        die(str(error))
    if ready is None:
        warn("等待超时，服务可能启动失败；请手动打开 %s（DSH 日志: %s）" % (URL, WEB_URL_LOG))
    return 0
