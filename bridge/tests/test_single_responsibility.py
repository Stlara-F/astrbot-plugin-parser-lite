"""功能唯一性守卫: 关键功能只能有一个实现, 防重复调用/重复定义.

设计原则: 每个叶子功能有且唯一 (单点事实来源):
- read_cfg             → cfg.py (唯一配置读取入口)
- 解析委托            → resolve.py (唯一上游 Parser 调用)
- clean_result_html    → render_patch.py (唯一渲染结果归一)
- format_full/brief    → format.py (唯一文本格式化)
- url 提取            → url_extract.py (唯一)
- 状态持久化 _load/save → 各状态类内 (模式统一, 无跨模块重复)
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BRIDGE = _ROOT / "bridge"

# 允许的生命周期/构造方法名 (各状态类标准模式, 非功能重复)
_ALLOWED_DUPS = {
    "__init__",
    "_load",
    "save",
    "start",
    "stop",
    "run",
    "cleanup",
    "arm",
    "concede",
    "disarm",
    "load_cfg",
}  # 各模块自包含配置段 (依赖注入模式, 职责各自唯一)


def _all_defs() -> dict[str, list[str]]:
    names: dict[str, list[str]] = defaultdict(list)
    for p in BRIDGE.glob("*.py"):
        if p.name.startswith("__"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    names[node.name].append(p.name)
    return names


def test_no_duplicate_functionality():
    """公开函数 (非下划线) 跨文件不得重复定义 — 功能有且唯一."""
    names = _all_defs()
    dups = {k: v for k, v in names.items() if len(v) > 1 and k not in _ALLOWED_DUPS}
    assert not dups, f"功能重复定义: {dups}"


def test_key_functions_single_owner():
    """关键叶子功能有且唯一实现."""
    owner_map = {
        "read_cfg": "cfg.py",
        "apply_downloader_proxy": "proxy.py",
        "clean_result_html": "render_patch.py",
        "pl_esc": "render_patch.py",
        "pl_str": "render_patch.py",
        "format_full": "format.py",
        "format_brief": "format.py",
        "extract_urls": "url_extract.py",
        "ParserLite": "resolve.py",
        "inject_dynamic_options_static": "inject.py",
        "sync_cookies_to_upstream": "proxy.py",
    }
    for fn, owner in owner_map.items():
        files = []
        for f in BRIDGE.glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            defined = any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == fn
                for n in ast.walk(tree)
            )
            if defined:
                files.append(f)
        owners = [f.name for f in files]
        assert owner in owners, f"{fn} 缺失于 {owner}"
        assert len(owners) == 1, f"{fn} 多实现: {owners}"


def test_config_read_single_entry():
    """配置读取唯一入口: main.py 不得直接 .get, 统一 _bridge_cfg."""
    main_py = (_ROOT / "main.py").read_text(encoding="utf-8")
    import re

    direct = re.findall(r"BridgeConfig\._source[^)]*\.get\(", main_py)
    assert not direct, f"main.py 存在直接配置读取: {direct}"
