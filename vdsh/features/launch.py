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
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .. import settings as settings_mod
from ..config import (
    AUTH_REQUIRED_MARKER,
    BOOT_MARKER,
    CLI_REL,
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
)
from ..console import die, say, step, warn
from ..spinner import Spinner

NAME = "launch"
SUMMARY = "启动 dsh web 并打开浏览器（默认命令）"


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


def wait_until_ready(deadline, gap, spinner_message=None, on_ready=None, log_path=None):
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
            time.sleep(gap)
            waited += gap
    finally:
        spinner.finish()
    if ready is not None and on_ready is not None:
        on_ready(ready)
    return ready


def register_workspace(path, session=None):
    """向已在运行的 Harness 实例注册工作区（POST /api/workspace.create，幂等）。

    0.1.3-alpha.1 起 /api 需要浏览器会话 cookie：传入已认证的 session（从
    启动日志的认证 URL 交换得到）才能通过；无 cookie 时后端返回 401。
    返回 True 表示注册成功；任何失败仅告警，不阻断打开浏览器。
    """
    envelope = {
        "type": "client-request",
        "rpcId": uuid.uuid4().hex,
        "method": "workspace.create",
        "payload": {"path": path},
    }
    client = session if session is not None else requests
    try:
        resp = client.post(
            "http://127.0.0.1:%d/api/workspace.create" % PORT,
            json=envelope,
            timeout=POLL_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        if resp.status_code == 401:
            warn("工作区注册需要浏览器认证（先用 DSH 控制台打印的 URL 打开一次页面）")
            return False
        data = resp.json()
        result = data.get("result")
        if result is not None and result.get("ok") is True:
            return True
        error = result.get("error") if isinstance(result, dict) else None
        code = error.get("code") if isinstance(error, dict) else "unknown"
        warn("工作区注册被拒绝（%s: %s）" % (
            code,
            error.get("message", "") if isinstance(error, dict) else "",
        ))
    except (requests.RequestException, ValueError) as error:
        warn("工作区注册失败（%s）" % error)
    return False


def open_url(url):
    os.startfile(url)


def spawn_server(repo, workspace, patch_path, tailnet):
    """弹独立最小化 pwsh 窗口启动 dsh web；stdout/stderr 全流重定向到 WEB_URL_LOG。

    0.1.3-alpha.1 起 dsh web 把「认证 URL 行」（dsh web: http://…/?token=…）打印到
    stdout 作为就绪信号；重定向后 vdsh 才能捕获它完成浏览器认证与就绪判定，
    DSH 控制台输出也落在该日志（排障时查看）。
    """
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        die("未找到 pwsh（PowerShell 7），请安装并加入 PATH", EXIT_NO_PWSH)
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
        subprocess.Popen(
            [pwsh, "-NoExit", "-WorkingDirectory", workspace, "-Command", command],
            cwd=workspace,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
    except OSError as error:
        die("无法弹出服务窗口（%s）" % error, EXIT_NO_PWSH)


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
    say("vdsh · 已在运行 → %s" % URL)
    if tailnet is not None:
        warn("运行中的实例未带 --trusted-host，手机访问会 403；"
             "请关闭后重启（vdsh --tailnet %s）" % tailnet)


def _open_launched(tailnet, want_browser, ready):
    authed_url = ready.get("authed_url")
    if want_browser:
        open_url(authed_url or URL)  # 打开带 token 的认证 URL（浏览器首访换 cookie）
    say("vdsh · 就绪 → %s" % URL)
    if tailnet is not None:
        token = _token_of(authed_url)
        if token:
            say("vdsh · 手机访问 → https://%s/?token=%s" % (tailnet, token))
        else:
            say("vdsh · 手机访问 → https://%s/" % tailnet)
    else:
        lan_url = ready.get("lan_url")
        if lan_url:
            say("vdsh · 手机访问 → %s" % lan_url)


def run(argv, settings):
    """启动功能入口：ready/starting/idle 状态机（默认命令）。"""
    from . import build as build_feature, sync as sync_feature

    payload = parse_launch_args(argv)
    repo = settings_mod.effective_repo(settings)
    if not os.path.isfile(os.path.join(repo, "package.json")):
        die("未找到 Harness 仓库（%s），请设置 DSH_REPO 或编辑 vdsh.yaml 的 launcher.repo" % repo,
            EXIT_USAGE)

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
                warn("运行中的实例启用了浏览器认证；若页面提示认证，"
                     "请从 DSH 控制台窗口（或 %s）打印的 URL 重新打开" % WEB_URL_LOG)
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
            spinner_message="检测到实例启动中，请稍候…",
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

    patch_path = ensure_seed_patch() if launcher_cfg["workspace_seed"] else None
    spawn_server(repo, workspace_path, patch_path, tailnet)
    step("启动中 · %s" % workspace_path)
    ready = wait_until_ready(
        deadline=launcher_cfg["startup_timeout_seconds"],
        gap=launcher_cfg["poll_gap_seconds"],
        spinner_message="服务启动中，请稍候…",
        log_path=WEB_URL_LOG,
        on_ready=lambda r: _open_launched(tailnet, launcher_cfg["open_browser"], r),
    )
    if ready is None:
        warn("等待超时，服务可能启动失败；请手动打开 %s（DSH 日志: %s）" % (URL, WEB_URL_LOG))
    return 0
