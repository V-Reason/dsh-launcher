# -*- coding: utf-8 -*-
"""profile 插件状态读取与校验（manifest / lockfile / 磁盘解析身份 / 生效方式）。

背景（2026-09-10）：`vdsh update plugin` 原先只比较「已装包 package.json 的 version」，
对 git 依赖（`github:` 规格）会假阴性——上游推了新 commit 却未升版本号时被误判为
「无版本变化」；也无法区分「装了/生效了/根本没被引用」。

本模块给出三重证据并判定生效方式：

1. `package.json`      声明：`dependencies` 与 `dsh.profile.bundles`；
2. `pnpm-lock.yaml`    pnpm 解析记录：`importers['.'].dependencies`（可缺省，见降级）；
3. `node_modules/.modules.yaml` 磁盘解析身份：`hoistedLocations` 的键
   `<name>@<version>` 或 `<name>@git+ssh://…#<commit>`（`nodeLinker: hoisted`，
   dsh 生成的 profile 固定为此布局）——**这才是「装的是哪个版本/哪个 commit」的权威记录**。

生效方式四态：`profile 层`（声明 `dsh.bundle.patch` 且在 bundles）/ `未激活`（声明了却不在
bundles，硬失败）/ `预设挂载`（未声明 bundle，但被预设或补丁层按包名引用）/ `普通依赖`。

纯标准库；PyYAML 仅用于 lockfile 交叉校验，缺失时降级跳过（打印 ⚠，不报错）。
"""

import json
import os
import re

LOCKFILE_NAME = "pnpm-lock.yaml"
MODULES_REL = os.path.join("node_modules", ".modules.yaml")
NODE_MODULES = "node_modules"

# 生效方式（中文文案，直接用于 doctor / update 输出）。
ACT_BUNDLE = "profile 层"
ACT_INACTIVE = "未激活"
ACT_PRESET = "预设挂载"
ACT_PLAIN = "普通依赖"

_COMMIT_RE = re.compile(r"#([0-9a-fA-F]{7,64})$")  # git SHA-1 40 位、SHA-256 64 位
_REF_SCAN_SUFFIXES = (".yml", ".yaml", ".json", ".md")
_REF_SCAN_MAX_BYTES = 512 * 1024


def _read_text(path, max_bytes=_REF_SCAN_MAX_BYTES):
    """读文本（容错）：不存在/不可读/超限 → None。"""
    try:
        if os.path.getsize(path) > max_bytes:
            return None
    except OSError:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_json(path):
    """读 JSON（容错）：不存在/非法 → None。"""
    text = _read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# ── 解析身份 ───────────────────────────────────────────────────────────────
def split_resolution(key):
    """拆 `hoistedLocations` 键为 (包名, 解析身份)。

    名字以 `@` 开头（scoped）时取首个 `/` 之后的第一个 `@`，否则取首个 `@`：
    `@liustack/modlens@3.26.1` 与 `dsh-at-file@git+ssh://git@github.com/x.git#abc`
    （URL 内还有 `@`）都必须正确拆分，不能用 rsplit。
    """
    start = key.index("/") + 1 if key.startswith("@") else 0
    at = key.find("@", start)
    if at < 0:
        return key, ""
    return key[:at], key[at + 1:]


def commit_of(resolution):
    """git 解析身份里的 commit（短哈希截断前），非 git → None。"""
    match = _COMMIT_RE.search(resolution or "")
    return match.group(1) if match else None


def identity(resolution):
    """比较用身份：git → ('git', commit)；其余 → ('semver', 版本串)。"""
    commit = commit_of(resolution)
    if commit is not None:
        return ("git", commit.lower())
    return ("semver", (resolution or "").split("(")[0].strip())


def short_id(resolution):
    """身份短写：git → 短 commit；其余 → 版本串（用于不一致提示）。"""
    commit = commit_of(resolution)
    if commit is not None:
        return commit[:7]
    return (resolution or "?").split("(")[0].strip() or "?"


def display(version, resolution):
    """人读身份：`0.18.1` / `0.7.0@da602d1`。

    git 依赖必须带短 commit：版本号相同、commit 不同正是「看起来没更新、其实更新了」
    的场景（也是旧实现假阴性的来源）。
    """
    commit = commit_of(resolution)
    if commit is None:
        return str(version or short_id(resolution))
    return "%s@%s" % (version or "?", commit[:7])


# ── 三份证据 ───────────────────────────────────────────────────────────────
def read_manifest(profile_dir):
    """profile/package.json → {present, deps, bundles}（缺失/非法时 present=False）。"""
    data = _read_json(os.path.join(profile_dir, "package.json"))
    if not isinstance(data, dict):
        return {"present": False, "deps": {}, "bundles": []}
    deps = data.get("dependencies")
    profile = (data.get("dsh") or {}).get("profile") or {}
    bundles = profile.get("bundles") or []
    return {
        "present": True,
        "deps": deps if isinstance(deps, dict) else {},
        "bundles": [str(b) for b in bundles] if isinstance(bundles, list) else [],
    }


def read_lockfile_deps(profile_dir):
    """lockfile 的 `importers['.'].dependencies` → {name: {specifier, version}}。

    PyYAML 缺失或解析失败 → None（调用方按「跳过交叉校验」降级处理）。
    """
    path = os.path.join(profile_dir, LOCKFILE_NAME)
    if not os.path.isfile(path):
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    importers = data.get("importers")
    if not isinstance(importers, dict):
        return None
    root = importers.get(".") or {}
    deps = root.get("dependencies") if isinstance(root, dict) else None
    if not isinstance(deps, dict):
        return {}
    return {name: value for name, value in deps.items() if isinstance(value, dict)}


def read_hoisted_ids(profile_dir):
    """磁盘解析身份：`.modules.yaml` 的 `hoistedLocations` → {name: resolution}。

    只取「被提升到 `node_modules/<name>`」的顶层条目（即 direct dependency 的位置）。
    文件缺失/无该字段 → None（校验降级为读已装包的 version 字段）。
    """
    data = _read_json(os.path.join(profile_dir, MODULES_REL))
    if not isinstance(data, dict):
        return None
    locations = data.get("hoistedLocations")
    if not isinstance(locations, dict):
        return None
    result = {}
    for key, paths in locations.items():
        if not isinstance(key, str) or not isinstance(paths, list):
            continue
        name, resolution = split_resolution(key)
        for entry in paths:
            normalized = str(entry).replace("\\", "/").strip("/")
            if normalized == "%s/%s" % (NODE_MODULES, name) and name not in result:
                result[name] = resolution
                break
    return result


def read_installed_manifest(profile_dir, name):
    """已装包的 package.json（容错）→ dict | None。"""
    data = _read_json(os.path.join(profile_dir, NODE_MODULES, name, "package.json"))
    return data if isinstance(data, dict) else None


def declares_bundle(pkg_manifest):
    """是否声明 `dsh.bundle.patch`（与 dsh app-boot 的 exportsPatch 判据一致）。"""
    if not isinstance(pkg_manifest, dict):
        return False
    dsh = pkg_manifest.get("dsh")
    if not isinstance(dsh, dict):
        return False
    bundle = dsh.get("bundle")
    return isinstance(bundle, dict) and "patch" in bundle


def looks_like_plugin(profile_dir, name):
    """是否是「插件形状」的包：名字以 dsh- 开头，或自带补丁文件/插件描述。

    用于把「未见引用」的告警限制在插件上，避免对普通库（clsx 之类）误报。
    """
    if name.startswith("dsh-"):
        return True
    base = os.path.join(profile_dir, NODE_MODULES, name)
    return any(os.path.isfile(os.path.join(base, marker))
               for marker in ("cordis.patch.yml", "dsh.plugin.json"))


# ── 预设 / 补丁层引用扫描 ──────────────────────────────────────────────────
def _preset_roots(data_dir, repo):
    roots = []
    if data_dir:
        roots.append(os.path.join(data_dir, ".agent-presets"))
    if repo:
        roots.append(os.path.join(repo, "agent-presets"))
        roots.append(os.path.join(repo, "packages", "preset", "agent-presets", "presets"))
    return [root for root in roots if os.path.isdir(root)]


def read_referenced_names(profile_dir, data_dir=None, repo=None):
    """收集「被引用过的包名」：预设目录 + profile 补丁层 + **其它**插件的补丁。

    判定故意宽松（子串命中即算引用）：宁可把「没人用」看成「有人用」，也不能把真正
    被预设挂载的插件误报为无人使用——后者的代价是假告警。但插件**自己**的补丁文件
    不算对自己的引用（否则任何自带 cordis.patch.yml 的插件都会「自我证明」被使用）。
    """
    deps = read_manifest(profile_dir)["deps"]
    sources = []  # (来源标签, 文本)；标签 = 包名表示「该包自己的补丁」
    for root in _preset_roots(data_dir, repo):
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(_REF_SCAN_SUFFIXES):
                    continue
                path = os.path.join(dirpath, filename)
                text = _read_text(path)
                if text:
                    sources.append(("preset:" + path, text))
    for filename in ("cordis.yml", "cordis.patch.yml"):
        text = _read_text(os.path.join(profile_dir, filename))
        if text:
            sources.append(("profile:" + filename, text))
    for name in deps:
        text = _read_text(os.path.join(profile_dir, NODE_MODULES, name, "cordis.patch.yml"))
        if text:
            sources.append((name, text))

    referenced = set()
    for name in deps:
        for label, text in sources:
            if label == name:
                continue
            if name in text:
                referenced.add(name)
                break
    return referenced


# ── 校验 ───────────────────────────────────────────────────────────────────
def verify(profile_dir, data_dir=None, repo=None):
    """校验一个 profile 的插件状态。

    返回 dict：
      present    profile 是否已初始化（package.json 存在）
      plugins    [{name, spec, resolution, display, identity, installed, activation}]
      failures   [文本]：硬失败（缺失 / 声明 bundle 却未激活 / 与 lockfile 不一致 / 未记入 lockfile）
      warnings   [文本]：需人工确认但不失败（疑似装了没被引用 / 校验降级）
      peer_hint  是否有插件声明 peerDependencies（doctor 用来给 pnpm peers check 指引）
    """
    manifest = read_manifest(profile_dir)
    if not manifest["present"]:
        return {"present": False, "plugins": [], "failures": [], "warnings": [], "peer_hint": False}

    deps = manifest["deps"]
    bundles = manifest["bundles"]
    lock_deps = read_lockfile_deps(profile_dir)
    hoisted = read_hoisted_ids(profile_dir)
    referenced = read_referenced_names(profile_dir, data_dir, repo)

    failures = []
    warnings = []
    if lock_deps is None:
        warnings.append("无法读取 %s：跳过 lockfile 交叉校验（缺 PyYAML 或文件缺失/非法）"
                        % os.path.join(profile_dir, LOCKFILE_NAME))
    if hoisted is None:
        warnings.append("node_modules/.modules.yaml 无 hoistedLocations："
                        "解析身份降级为已装包 version（git 依赖的 commit 变化无法核验）")

    plugins = []
    peer_hint = False
    for name in sorted(deps):
        pkg = read_installed_manifest(profile_dir, name)
        installed = pkg is not None
        if pkg is not None and isinstance(pkg.get("peerDependencies"), dict) and pkg["peerDependencies"]:
            peer_hint = True

        resolution = (hoisted or {}).get(name)
        degraded = resolution is None
        if degraded and installed:
            resolution = str(pkg.get("version") or "")
        resolution = resolution or ""
        version = str((pkg or {}).get("version") or "")

        if declares_bundle(pkg):
            activation = ACT_BUNDLE if name in bundles else ACT_INACTIVE
        elif name in referenced:
            activation = ACT_PRESET
        else:
            activation = ACT_PLAIN

        plugins.append({
            "name": name,
            "spec": deps[name],
            "resolution": resolution,
            "display": display(version, resolution) if resolution else "?",
            "identity": identity(resolution) if resolution else ("unknown", ""),
            "installed": installed,
            "activation": activation,
        })

        if not installed:
            failures.append("%s 在 package.json 中声明但未安装（cd %s; pnpm install）"
                            % (name, profile_dir))
            continue
        if activation == ACT_INACTIVE:
            failures.append("%s 声明了 dsh.bundle.patch 但不在 dsh.profile.bundles —— "
                            "该版本不会作为 profile 层生效" % name)
        if activation == ACT_PLAIN and looks_like_plugin(profile_dir, name):
            warnings.append("%s 已安装但未见 bundle/preset 引用（普通依赖，该更新可能不会生效；"
                            "如属预期可忽略）" % name)

        if lock_deps is None:
            continue
        entry = lock_deps.get(name)
        if entry is None:
            failures.append("%s 在 package.json 中声明但 pnpm-lock.yaml 未记录"
                            "（cd %s; pnpm install）" % (name, profile_dir))
            continue
        locked = str(entry.get("version") or "")
        disk_id, locked_id = identity(resolution), identity(locked)
        # 只在两侧身份同类时比对：降级路径（无 hoistedLocations）拿到的 semver 与 lockfile
        # 的 git 身份不可比，强行比对会制造假失败。
        if degraded or locked_id[0] != disk_id[0]:
            continue
        if locked and disk_id != locked_id:
            failures.append("%s 磁盘解析 %s 与 pnpm-lock.yaml 记录 %s 不一致"
                            "（cd %s; pnpm install）"
                            % (name, short_id(resolution), short_id(locked), profile_dir))

    return {
        "present": True,
        "plugins": plugins,
        "failures": failures,
        "warnings": warnings,
        "peer_hint": peer_hint,
    }


def diff_plugins(before, after):
    """对比两次 verify 的插件快照 → (updated, added, removed, unchanged)。

    updated 元素为 (name, 旧 display, 新 display)；判定用 identity（含 git commit），
    因此「版本号没变但 commit 变了」会正确判为更新。
    """
    old = {p["name"]: p for p in before.get("plugins", [])}
    new = {p["name"]: p for p in after.get("plugins", [])}
    updated = [(name, old[name]["display"], new[name]["display"])
               for name in sorted(new)
               if name in old and old[name]["identity"] != new[name]["identity"]]
    added = sorted(name for name in new if name not in old)
    removed = sorted(name for name in old if name not in new)
    unchanged = sorted(name for name in new
                       if name in old and old[name]["identity"] == new[name]["identity"])
    return updated, added, removed, unchanged


def activation_summary(state):
    """生效方式计数文案：`profile 层 4 · 预设挂载 1`。"""
    counts = {}
    for plugin in state.get("plugins", []):
        counts[plugin["activation"]] = counts.get(plugin["activation"], 0) + 1
    parts = ["%s %d" % (key, counts[key])
             for key in (ACT_BUNDLE, ACT_PRESET, ACT_PLAIN, ACT_INACTIVE) if counts.get(key)]
    return " · ".join(parts) if parts else "无"
