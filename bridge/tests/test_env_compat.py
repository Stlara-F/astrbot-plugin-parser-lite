"""多端环境兼容性测试 — 辅助编码: 验证 bridge 模块在多种环境可导入.

环境矩阵:
- 无 astrbot (CI): 模块顶层不得 import astrbot (组件/logger 延迟)
- 无上游 standalone: context.up_* 延迟加载, 配置类可导入
- 解耦回归: 上游 src 模块源码不得引用 bridge
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent


def _clean_sys_modules():
    """清除 bridge/astrbot 模块缓存, 模拟纯净环境."""
    for name in list(sys.modules):
        if name.startswith(("bridge", "astrbot", "nonebot_plugin_parser_lite")):
            sys.modules.pop(name, None)


def test_no_top_level_astrbot_import():
    """CI 兼容: bridge 模块顶层不得 import astrbot (组件延迟).

    只检查顶层 Import 语句 (非函数内), 保证 CI 无 astrbot 可导入.
    """
    for f in (_ROOT / "bridge").glob("*.py"):
        if f.name.startswith("__") or f.name == "tests":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                # 顶层 (非函数内) 的 astrbot import
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    names = [node.module or ""]
                top_level = all(
                    not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for p in _parents(tree, node)
                )
                if top_level and any(n.startswith("astrbot") for n in names):
                    raise AssertionError(
                        f"{f.name}: 顶层 import astrbot (须延迟到函数内): {names}")


def _parents(tree, node):
    """收集 node 的全部祖先 (递归)."""
    found = set()

    def walk(parents):
        parent = parents[-1]
        for child in ast.iter_child_nodes(parent):
            if child is node:
                found.update(parents)
            walk([*parents, child])

    walk([tree])
    return found


def test_bridge_importable_without_astrbot():
    """无 astrbot 环境: 全部 bridge 模块可导入 (模拟 CI)."""
    _clean_sys_modules()
    import importlib

    modules = [
        "bridge.cfg", "bridge.context", "bridge.inject", "bridge.proxy",
        "bridge.resolve", "bridge.custom_parser", "bridge.send",
        "bridge.format", "bridge.url_extract", "bridge.debounce",
        "bridge.rate_limit", "bridge.delay_send", "bridge.arbiter",
        "bridge.push", "bridge.cookie_health", "bridge.card_semantic",
        "bridge.fallback", "bridge.doctor",
    ]
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if "astrbot" in str(e):
                raise AssertionError(f"{mod}: 导入依赖 astrbot (顶层): {e}")
            # 上游 standalone 缺失是可接受的 (CI 有)
            raise


def test_upstream_not_import_bridge():
    """解耦回归: 上游 src 模块源码不得引用 bridge."""
    src_dir = _ROOT / "src" / "nonebot_plugin_parser_lite"
    if not src_dir.exists():
        return  # CI 无上游时跳过
    offenders = []
    for f in src_dir.rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\bimport bridge\b|\bfrom bridge\b", text):
            offenders.append(str(f.relative_to(_ROOT)))
    assert not offenders, f"上游模块反向依赖 bridge: {offenders}"


def test_upstream_references_only_public_api():
    """桥接只调用上游公开 API (薄桥接边界): 禁止 bridge 引用上游私有模块.

    黑名单: matchers (nonebot 生态), helper 内部, 非公开模块.
    """
    blacklist = ("matchers",)
    for f in (_ROOT / "bridge").glob("*.py"):
        if f.name.startswith("__") or f.name == "tests":
            continue
        text = f.read_text(encoding="utf-8")
        for mod in blacklist:
            if f"nonebot_plugin_parser_lite.{mod}" in text:
                raise AssertionError(f"{f.name}: bridge 引用了上游私有模块 {mod}")
