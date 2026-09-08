# -*- coding: utf-8 -*-
"""dsh profile 模块回退目录自愈（纯标准库，launch 与 dsh_cli 共用）。

背景：dsh 把「安装回退」放在 `$DSH_HOME/profiles/*/.dsh-module-fallback/node_modules`，
里面是操作系统级链接（junction / symlink）或 dsh 管理的 proxy 目录
（目录内 package.json 声明 `dsh.moduleFallback.targets`）。Git 同步（dsh-data-git-sync）
在 Windows 上会把 junction 当作普通目录**递归展开**提交；副机 checkout 后得到的是
真实目录/文件而非链接，dsh 启动时 healProfileModuleFallback → ensureSymlink 会拒绝：

    dsh: <路径> exists and is not a symlink or dsh-managed module proxy

本模块按 app-boot 的判定规则清理「既非链接也非 proxy」的条目（这类条目必然是
同步污染，dsh 启动时会自动重建），供 vdsh 启动前 / dsh 命令转发前调用。
"""

import json
import os
import shutil

# Windows 文件系统标志（reparse point = 链接/junction 的存档位）。
_FILE_ATTR_REPARSE_POINT = 0x400


def _is_linkish(path):
    """判断 path 是否为 symlink 或 junction（Windows reparse point）。"""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    # Python < 3.12 兜底：lstat 的 reparse 标志位。
    try:
        stub = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(stub, "st_reparse_tag", 0)) or bool(
        getattr(stub, "st_file_attributes", 0) & _FILE_ATTR_REPARSE_POINT)


def _is_proxy(path):
    """dsh 管理的 proxy 目录：内含 package.json 且声明 dsh.moduleFallback.targets。"""
    manifest = os.path.join(path, "package.json")
    if not os.path.isfile(manifest):
        return False
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    dsh = data.get("dsh")
    if not isinstance(dsh, dict):
        return False
    fallback = dsh.get("moduleFallback")
    if not isinstance(fallback, dict):
        return False
    # 与 app-boot 一致：targets 键存在即视为 dsh 管理（含 null/[]，仅 undefined 除外）。
    return "targets" in fallback


def _is_empty_dir(path):
    try:
        return os.path.isdir(path) and not os.listdir(path)
    except OSError:
        return False


def _remove_entry(path, removed):
    """删除单个非法条目（目录递归 / 文件），返回新增清理计数。"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return removed + 1
    except OSError:
        return removed  # 删除失败不阻断启动；残留条目仍可能让 dsh 报错，但不再尝试


def _heal_scope_dir(scope_path, removed):
    """清理 @scope 目录内非链接、非 proxy 的子条目。

    @scope 目录本身可能是普通目录（内含 junction 子项，合法）也可能是同步
    污染（内含真实目录/文件）。逐子项判定，避免误删合法 scope。
    """
    try:
        children = sorted(os.listdir(scope_path))
    except OSError:
        return removed
    for child in children:
        entry = os.path.join(scope_path, child)
        if _is_linkish(entry) or (_is_proxy(entry) and os.path.isdir(entry)):
            continue
        removed = _remove_entry(entry, removed)
    # 清理后 scope 若为空壳（既无合法子项也无污染），删除它；否则保留。
    if _is_empty_dir(scope_path) and not _is_linkish(scope_path):
        try:
            os.rmdir(scope_path)
            removed += 1
        except OSError:
            pass
    return removed


def heal_module_fallback(data_dir, reporter=None):
    """扫描 data_dir 下各 profile 的模块回退目录，删除非链接、非 proxy 条目。

    data_dir: DSH 数据目录（$DSH_HOME / ~/.dsh）。
    reporter: 可选回调 removed(removed_count)（在清理数 > 0 时调用一次）。
    返回清理条目数（0 = 无污染）。从不触碰合法链接与 proxy。
    """
    if not data_dir:
        return 0
    profiles_dir = os.path.join(data_dir, "profiles")
    if not os.path.isdir(profiles_dir):
        return 0
    removed = 0
    try:
        profile_names = sorted(os.listdir(profiles_dir))
    except OSError:
        return 0
    for name in profile_names:
        profile_dir = os.path.join(profiles_dir, name)
        if not os.path.isdir(profile_dir) or name == "node_modules":
            continue  # profiles/node_modules 是共享层，非 profile
        fallback = os.path.join(profile_dir, ".dsh-module-fallback")
        modules = os.path.join(fallback, "node_modules")
        if not os.path.isdir(modules):
            continue
        try:
            entries = sorted(os.listdir(modules))
        except OSError:
            continue
        for entry in entries:
            item = os.path.join(modules, entry)
            if _is_linkish(item):
                continue
            if entry.startswith("@"):
                if os.path.isdir(item):
                    removed = _heal_scope_dir(item, removed)
                else:
                    removed = _remove_entry(item, removed)
                continue
            if os.path.isdir(item) and _is_proxy(item):
                continue
            removed = _remove_entry(item, removed)
    if removed > 0 and reporter is not None:
        reporter(removed)
    return removed
