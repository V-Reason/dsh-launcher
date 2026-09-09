# -*- coding: utf-8 -*-
"""固定环境信息、阈值与退出码的唯一来源。"""

import shutil
import tempfile
from pathlib import Path

# 启动器根目录（vdsh/ 包的上一级；与 vdsh_launcher.py 同目录）。
LAUNCHER_DIR = Path(__file__).resolve().parent.parent

# ── Harness 仓库与就绪标记 ────────────────────────────────────────────────
DEFAULT_REPO = r"T:\deepseek-harness"
# 本机候选仓库（附 DSH_REPO / vdsh.yaml 之后自动探测；跨机复制 launcher 时自愈）。
# 顺序即优先级：先本机常见路径（副机 C:\deepseek-harness），再历史默认（T:）。
REPO_CANDIDATES = [r"C:\deepseek-harness", DEFAULT_REPO]
PORT = 3080
URL = "http://127.0.0.1:%d/" % PORT

# 就绪判定主标记：dsh web 服务必然向页面注入 window.__DSH_BOOT__ 引导清单
# （0.1.1-rc.2 起标题品牌化后依然稳定存在）；
# 旧标题仅作兼容回退，用于识别仍在运行的旧构建实例。
BOOT_MARKER = "__DSH_BOOT__"
TITLE_MARKER = "DeepSeek Harness"
# 0.1.3-alpha.1（2026-08-30 重构）起，dsh web 对根页面与 /api 实施浏览器会话认证：
# GET / 无 ?token= 或 cookie → 401，正文含此串（据此识别"已在运行"的实例）。
AUTH_REQUIRED_MARKER = "dsh web authentication required"

# Connection RPC 通道契约（源码：client/connection/src/rpc-host.ts、api/gateway/src/index.ts）：
#   端点路径 = /api/<namespace>/<method>（点号写法不存在，得到 404 纯文本）；
#   请求体   = {"type":"client-request","rpcId":…,"method":"<namespace>/<method>",
#               "payload":{"args":{"request":{…}}}}（args 下的键 = 方法形参名，见
#               workspace-controller 的 @Remote('create') create(request)）。
# /api 在端点分发之前先过两道闸：Host 围栏（未声明域名 → 403）与浏览器会话认证（无 cookie → 401）。
WORKSPACE_CREATE_ENDPOINT = "workspace/create"
API_RPC_URL = "http://127.0.0.1:%d/api" % PORT

# dsh web 启动日志（stdout/stderr 全流重定向处）：捕获「认证 URL 行」
# （dsh web: http://127.0.0.1:3080/?token=…，Loader 结算后打印的就绪信号）与
# DSH 控制台输出（排障时查看）。
WEB_URL_LOG = Path(tempfile.gettempdir()) / "vdsh-web.log"

CLI_REL = str(Path("apps", "cli", "lib", "bin.js"))
DIST_REL = str(Path("apps", "web", "dist", "index.html"))
SRC_DIRS = (str(Path("apps", "cli", "src")), str(Path("apps", "web", "src")))

BUILD_PROMPT = "检测构建产物缺失/源码更新，执行build? [Y/n] "

# ── 就绪轮询预算（插件较多时启动可能超过 1 分钟） ───────────────────────────
MAX_POLLS = 360
POLL_TIMEOUT = 1.0
POLL_GAP = 0.5
TCP_TIMEOUT = 0.5
# 端口已占用但页面未就绪时（可能仍在上次启动中）的等待上限（秒）。
STARTING_WAIT_BUDGET = 30.0
STARTING_WAIT_GAP = 2.0

# ── 工作区种子（本目录用户侧文件，非 Harness 仓库源码） ─────────────────────
SEED_MJS = LAUNCHER_DIR / "workspace-seed.mjs"
SEED_YML = LAUNCHER_DIR / "seed.yml"

# ── 用户配置（vdsh.yaml；缺失 = 全部回退内置默认，见 vdsh/settings.py） ──────
CONFIG_PATH = LAUNCHER_DIR / "vdsh.yaml"

# ── DSH 数据同步（dsh-data-git-sync，本目录子包） ──────────────────────────
SYNC_DIR = LAUNCHER_DIR / "dsh-data-git-sync"
SYNC_PS1 = SYNC_DIR / "sync-dsh.ps1"
# 同步脚本退出码语义（见 sync-dsh.ps1）：0 成功 / 1 硬失败 / 2 用法错误 /
# 3 被阻塞（脏工作区、冲突） / 4 未初始化（可跳过）。
SYNC_OK, SYNC_BLOCKED, SYNC_NOT_SETUP = 0, 3, 4


def sync_host():
    """同步脚本宿主：Windows PowerShell 5.1（脚本 #Requires 5.1 兼容）。

    按需解析 PATH（而非导入时求值），避免 PATH 变化/装新环境时取到旧值。
    """
    return shutil.which("powershell") or "powershell"


# ── 启动器自身退出码 ───────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2        # 参数/环境错误（repo 缺失、未知参数、目录不存在）
EXIT_BUILD = 3        # 构建失败或 pnpm 缺失
EXIT_DEPS = 4         # Python 依赖缺失（requests）
EXIT_NO_PWSH = 5      # pwsh（PowerShell 7）缺失
EXIT_PORT_BUSY = 6    # 端口被占用但未识别为 Harness
