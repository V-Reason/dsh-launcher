# -*- coding: utf-8 -*-
"""vdsh.yaml 统一配置：加载 / 校验 / 合并 / 模板生成 / 报告。

语义：
  - 配置文件缺失 = 完全回退内置默认值（行为与无配置文件时一致）。
  - 优先级（文档化）：CLI 参数 > 环境变量（DSH_REPO / DSH_TAILNET_HOST / DSH_HOME）
    > vdsh.yaml > 内置默认。
  - 未知键 / 非法值：打印警告并回退该键默认值；YAML 整体解析失败：全部回退内置默认。
  - PyYAML 为惰性依赖：仅当配置文件存在时才需要（缺失时打印安装提示并退出）。
"""

import copy
import json
import os
import re
import sys

from .config import (
    CONFIG_PATH,
    DEFAULT_REPO,
    MAX_POLLS,
    POLL_GAP,
    STARTING_WAIT_BUDGET,
)


def normalize_tailnet(host):
    """规范化 tailnet 域名：去 scheme、尾斜杠、尾点（tailscale status 输出带尾点），转小写。"""
    if host is None:
        return None
    host = host.strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/").rstrip(".")
    return host or None

# ── 内置默认（与 vdsh.yaml 模板一一对应；模板生成自本结构）──────────────────
DEFAULTS = {
    "launcher": {
        "repo": DEFAULT_REPO,
        "tailnet": "",
        "startup_timeout_seconds": int(MAX_POLLS * POLL_GAP),
        "starting_budget_seconds": int(STARTING_WAIT_BUDGET),
        "poll_gap_seconds": POLL_GAP,
        "open_browser": True,
        "workspace_seed": True,
    },
    "animation": {
        "fps": 8,
        "frames": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    },
    "sync": {
        "data_dir": "",
        "remote": "",
        "allowlist": [
            ".gitignore", "sessions", "profiles/web", "storages",
            "attachments", "memories", "settings.yaml",
        ],
        "gitignore_extra": "",
        "commit_name": "DSH Sync",
        "commit_email": "dsh-sync@local",
        "timeout_seconds": 0,
    },
}

# ── 校验器（返回 True = 合法；False = 警告并回退默认）──────────────────────
def _valid_int1(value):
    """≥1 的整数。"""
    return isinstance(value, int) and value >= 1


def _valid_int0(value):
    """≥0 的整数。"""
    return isinstance(value, int) and value >= 0


def _valid_pos_number(value):
    return isinstance(value, (int, float)) and value > 0


def _valid_fps(value):
    return isinstance(value, int) and 1 <= value <= 60


def _valid_frames(value):
    return isinstance(value, str) and len(value) >= 2


def _valid_bool(value):
    return isinstance(value, bool)


def _valid_str(value):
    return isinstance(value, str)


def _valid_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


# (section, key) → (validator, 空串是否视为「未设置」)
VALIDATORS = {
    ("launcher", "repo"): (_valid_str, True),
    ("launcher", "tailnet"): (_valid_str, True),
    ("launcher", "startup_timeout_seconds"): (_valid_int1, False),
    ("launcher", "starting_budget_seconds"): (_valid_int0, False),
    ("launcher", "poll_gap_seconds"): (_valid_pos_number, False),
    ("launcher", "open_browser"): (_valid_bool, False),
    ("launcher", "workspace_seed"): (_valid_bool, False),
    ("animation", "fps"): (_valid_fps, False),
    ("animation", "frames"): (_valid_frames, False),
    ("sync", "data_dir"): (_valid_str, True),
    ("sync", "remote"): (_valid_str, True),
    ("sync", "allowlist"): (_valid_str_list, False),
    ("sync", "gitignore_extra"): (_valid_str, False),
    ("sync", "commit_name"): (_valid_str, True),
    ("sync", "commit_email"): (_valid_str, True),
    ("sync", "timeout_seconds"): (_valid_int0, False),
}

# 模板（各键 = 当前内置默认值；删除某行 = 该键回退内置默认。
# 注意：raw 字符串，不能以反斜杠转义开头——首行直接写内容。）
TEMPLATE = r"""# vdsh.yaml — vdsh 启动器 + DSH 数据同步统一配置
# 首次运行自动生成；每行都等于当前内置默认值，按需修改即可。
# 删除某行 = 该键回退内置默认；改完用 `vdsh config` 查看生效值。
# 优先级：命令行参数 > 环境变量(DSH_REPO/DSH_TAILNET_HOST/DSH_HOME) > 本文件 > 内置默认。
# 注意：sync.allowlist / sync.gitignore_extra 影响两端共享仓库内容，多机需保持一致。
launcher:
  repo: T:\deepseek-harness              # 未设置/留空 → DSH_REPO → 内置默认（无反斜杠转义问题，勿加引号）
  tailnet: ""                             # 未设置/留空 → DSH_TAILNET_HOST
  startup_timeout_seconds: 180            # 服务就绪等待上限（秒）
  starting_budget_seconds: 30             # 端口被占但未就绪时的等待上限（秒）
  poll_gap_seconds: 0.5                   # 就绪轮询间隔（秒）
  open_browser: true                      # false = 就绪后不自动打开浏览器（仍打印地址）
  workspace_seed: true                    # false = 不注入工作区种子插件
animation:
  fps: 8                                  # TTY 动画帧率（1-60）
  frames: "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"          # 动画帧序列（任意非空字符串）
sync:
  data_dir: ""                            # 数据目录；空 = DSH_HOME → ~/.dsh
  remote: ""                              # 默认远端；vdsh sync init 无参时使用
  allowlist: [.gitignore, sessions, profiles/web, storages, attachments, memories, settings.yaml]
  gitignore_extra: ''                     # 追加到自动生成的 .gitignore（空 = 不追加）
  commit_name: "DSH Sync"                 # 提交者身份（两端一致）
  commit_email: "dsh-sync@local"
  timeout_seconds: 0                      # 0 = 不限时；>0 时 fetch/push 等超过即终止（退出码 1）
"""


def ensure_config_template():
    """首次运行生成带注释的默认模板（幂等；生成失败仅告警，不阻断）。"""
    if CONFIG_PATH.exists():
        return
    try:
        CONFIG_PATH.write_text(TEMPLATE, encoding="utf-8")
    except OSError as error:
        print("vdsh ⚠ 无法写入配置模板 %s（%s），本次使用内置默认值。" % (CONFIG_PATH, error),
              file=sys.stderr)


def load_settings():
    """读取并校验 vdsh.yaml，返回 (settings, warnings)。

    settings 始终为完整 dict（缺失键 = 内置默认）；warnings 为提示列表（非空时由调用方打印）。
    配置文件不存在时返回纯默认值、无警告。
    """
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULTS), []
    try:
        import yaml
    except ImportError:
        print("vdsh ✗ 发现 %s，但未安装 PyYAML（pip install pyyaml）" % CONFIG_PATH,
              file=sys.stderr)
        sys.exit(4)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except Exception as error:
        return copy.deepcopy(DEFAULTS), [
            "vdsh ⚠ 无法解析 %s（%s），本次使用内置默认值。" % (CONFIG_PATH, error)]

    settings = copy.deepcopy(DEFAULTS)
    warnings = []
    if raw is None:
        return settings, warnings
    if not isinstance(raw, dict):
        return settings, ["vdsh ⚠ %s 顶层应为键值映射，本次使用内置默认值。" % CONFIG_PATH]

    for section, values in raw.items():
        if section not in DEFAULTS:
            warnings.append("vdsh ⚠ %s: 未知配置段 '%s'（已忽略）" % (CONFIG_PATH, section))
            continue
        if values is None:
            continue
        if not isinstance(values, dict):
            warnings.append("vdsh ⚠ %s: 配置段 '%s' 应为键值映射（已忽略）" % (CONFIG_PATH, section))
            continue
        for key, value in values.items():
            rule = VALIDATORS.get((section, key))
            if rule is None:
                warnings.append("vdsh ⚠ %s: 未知配置键 '%s.%s'（已忽略）" % (CONFIG_PATH, section, key))
                continue
            validator, empty_means_unset = rule
            if value is None or (empty_means_unset and value == ""):
                continue  # 未设置 → 保持内置默认
            if not validator(value):
                warnings.append(
                    "vdsh ⚠ %s: '%s.%s' 值非法（%r），已回退默认 %r"
                    % (CONFIG_PATH, section, key, value, DEFAULTS[section][key]))
                continue
            settings[section][key] = value
    return settings, warnings


def effective_repo(settings):
    """Harness 仓库根目录：DSH_REPO 环境变量 > vdsh.yaml > 内置默认。"""
    return os.environ.get("DSH_REPO") or settings["launcher"]["repo"] or DEFAULT_REPO


def effective_tailnet(cli_tailnet, settings):
    """tailnet 域名：CLI（已规范化）> DSH_TAILNET_HOST > vdsh.yaml（空/None = 未设置）。"""
    if cli_tailnet:
        return cli_tailnet
    return (normalize_tailnet(os.environ.get("DSH_TAILNET_HOST"))
            or normalize_tailnet(settings["launcher"]["tailnet"]))


def patch_values(replacements):
    """文本级写入 vdsh.yaml 的若干标量键（保留注释与其余内容）。

    replacements：{(section, key): value}；值经 json.dumps 转义为合法 YAML
    双引号标量（支持任意字符）。段或键不存在时在对应段内追加（段不存在则
    在文件末尾新建）。返回 (ok, warnings)；文件不存在时 ok=False。
    """
    if not CONFIG_PATH.exists():
        return False, ["vdsh ⚠ 配置文件不存在：%s" % CONFIG_PATH]
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as error:
        return False, ["vdsh ⚠ 无法读取配置文件（%s）" % error]

    for (section, key), value in replacements.items():
        escaped = json.dumps(value, ensure_ascii=False)
        line_pattern = re.compile(r"(?m)^([ \t]+)(%s):[^\r\n]*" % re.escape(key))
        match = line_pattern.search(text)
        if match:
            replacement = match.group(1) + key + ": " + escaped
            text = text[:match.start()] + replacement + text[match.end():]
            continue
        # 键不存在：段内追加（段不存在则新建段）。
        block = "  %s: %s\n" % (key, escaped)
        section_pattern = re.compile(r"(?m)^%s:[ \t]*\r?\n" % re.escape(section))
        section_match = section_pattern.search(text)
        if section_match:
            insert_at = section_match.end()
            text = text[:insert_at] + block + text[insert_at:]
        else:
            text = text.rstrip() + "\n\n%s:\n%s" % (section, block)

    try:
        CONFIG_PATH.write_text(text, encoding="utf-8")
    except OSError as error:
        return False, ["vdsh ⚠ 无法写入配置文件（%s）" % error]
    return True, []


def effective_data_dir(settings):
    """DSH 数据目录：DSH_HOME 环境变量 > vdsh.yaml sync.data_dir > None（脚本回退 ~/.dsh）。"""
    return os.environ.get("DSH_HOME") or settings["sync"]["data_dir"] or None


def config_report(settings):
    """`vdsh config` 输出：生效配置 + 来源标注。警告由调用方另行打印。"""
    lines = []
    if CONFIG_PATH.exists():
        lines.append("配置文件: %s" % CONFIG_PATH)
    else:
        lines.append("配置文件: （未创建——全部为内置默认值；将随下次运行自动生成模板）")
    lines.append("")
    lines.append("launcher:")
    launcher = settings["launcher"]
    env_repo = os.environ.get("DSH_REPO")
    lines.append("  repo: %s%s" % (
        os.environ.get("DSH_REPO") or launcher["repo"],
        "  <-- 环境变量 DSH_REPO" if env_repo else ""))
    tailnet = effective_tailnet(None, settings)
    lines.append("  tailnet: %s" % (tailnet or "（未设置）"))
    lines.append("  startup_timeout_seconds: %d" % launcher["startup_timeout_seconds"])
    lines.append("  starting_budget_seconds: %d" % launcher["starting_budget_seconds"])
    lines.append("  poll_gap_seconds: %s" % launcher["poll_gap_seconds"])
    lines.append("  open_browser: %s" % launcher["open_browser"])
    lines.append("  workspace_seed: %s" % launcher["workspace_seed"])
    lines.append("")
    lines.append("animation:")
    lines.append("  fps: %d" % settings["animation"]["fps"])
    lines.append("  frames: %s" % settings["animation"]["frames"])
    lines.append("")
    lines.append("sync:")
    data_dir = effective_data_dir(settings)
    lines.append("  data_dir: %s" % (data_dir or "（默认 ~/.dsh）"))
    lines.append("  remote: %s" % (settings["sync"]["remote"] or "（未设置）"))
    lines.append("  allowlist: %s" % ", ".join(settings["sync"]["allowlist"]))
    extra = settings["sync"]["gitignore_extra"]
    lines.append("  gitignore_extra: %s" % ("（未设置）" if not extra else extra.replace("\n", "\\n")))
    lines.append("  commit_name: %s" % settings["sync"]["commit_name"])
    lines.append("  commit_email: %s" % settings["sync"]["commit_email"])
    lines.append("  timeout_seconds: %d" % settings["sync"]["timeout_seconds"])
    return "\n".join(lines)
