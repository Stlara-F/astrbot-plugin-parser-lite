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
                        f"{f.name}: 顶层 import astrbot (须延迟到函数内): {names}"
                    )


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
    """无 astrbot 环境: 全部 bridge 模块可导入 (模拟 CI, 子进程隔离避免污染)."""
    import subprocess
    import sys

    _code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_ROOT / 'src')!r})\n"
        f"sys.path.insert(0, {str(_ROOT)!r})\n"
        "for k in list(sys.modules):\n"
        "    if k.startswith('astrbot'):\n"
        "        del sys.modules[k]\n"
        "mods = ['bridge.cfg','bridge.context','bridge.inject','bridge.proxy',\n"
        "        'bridge.resolve','bridge.send',\n"
        "        'bridge.format','bridge.url_extract',\n"
        "        'bridge.fallback','bridge.doctor']\n"
        "import importlib\n"
        "try:\n"
        "    for m in mods:\n"
        "        importlib.import_module(m)\n"
        "    print('OK')\n"
        "except ModuleNotFoundError as e:\n"
        "    print('FAIL', m, str(e))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", _code], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"子进程退出码非 0: {r.stderr[-300:]}"
    assert r.stdout.strip() == "OK", (
        f"子进程纯净导入失败: {r.stdout[-300:]}{r.stderr[-300:]}"
    )


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
