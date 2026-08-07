"""防重复导入守卫测试 — 指令冲突根因 (cmd_blogin 双注册).

验证:
- 同文件以两个模块名存在时, 第二次导入检测到重复
- 守卫算法正确识别 data.plugins.X.main vs 顶层 main
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import types

import pytest


def _load_guard_snippet(main_py: Path) -> str:
    """提取 main.py 中的守卫代码段 (无 astrbot 依赖, 可直接执行验证)."""
    text = main_py.read_text(encoding="utf-8")
    start = text.index("_PL_THIS_FILE = os.path.abspath(__file__)")
    end = text.index("_plugin_dir = os.path.dirname(os.path.abspath(__file__))")
    if end == -1:
        end = text.index("# AstrBot 插件根目录")
    return text[start:end]


def _exec_guard(
    tmp_path: Path,
    duplicate_module_names: list[str],
    current_name: str,
) -> list[str]:
    """在隔离的临时文件上模拟守卫 (避免真实 main.py 污染)."""
    fake_main = tmp_path / "main.py"
    fake_main.write_text("", encoding="utf-8")
    f = str(fake_main)
    for name in duplicate_module_names:
        m = types.ModuleType(name)
        m.__file__ = f
        sys.modules[name] = m

    ns: dict = {"sys": sys, "os": os}
    src = (
        "_PL_THIS_FILE = os.path.abspath(__file__)\n"
        "_PL_DUPLICATE_IMPORTS = [\n"
        "    m.__name__\n"
        "    for m in list(sys.modules.values())\n"
        "    if m is not sys.modules.get(__name__)\n"
        "    and getattr(m, '__file__', None)\n"
        "    and os.path.abspath(m.__file__) == _PL_THIS_FILE\n"
        "]\n"
    )
    ns["__file__"] = f
    ns["__name__"] = current_name
    sys.modules[current_name] = types.ModuleType(current_name)
    try:
        exec(src, ns)
        return ns["_PL_DUPLICATE_IMPORTS"]
    finally:
        for name in (*duplicate_module_names, current_name):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _clean_modules():
    yield
    for name in list(sys.modules):
        if name in ("main", "data.plugins.parser_lite.main"):
            sys.modules.pop(name, None)


def test_guard_detects_second_import(tmp_path):
    """顶层 main 二次导入 → 检测到 data.plugins.X.main 已加载."""
    dups = _exec_guard(tmp_path, [], "data.plugins.parser_lite.main")
    assert dups == []
    dups2 = _exec_guard(tmp_path, ["data.plugins.parser_lite.main"], "main")
    assert dups2 == ["data.plugins.parser_lite.main"]


def test_guard_no_false_positive(tmp_path):
    """无重复时返回空."""
    dups = _exec_guard(tmp_path, [], "main")
    assert dups == []


def test_main_py_contains_guard():
    """main.py 实际包含守卫代码 (回归保护)."""
    main_py = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / "main.py"
    text = main_py.read_text(encoding="utf-8")
    assert "_PL_THIS_FILE" in text
    assert "_PL_DUPLICATE_IMPORTS" in text
    assert "拒绝二次注册" in text


def test_main_py_guard_runs_before_registration():
    """守卫位于 sys.path 注入与 astrbot import 之前 (尽早阻断)."""
    main_py = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / "main.py"
    text = main_py.read_text(encoding="utf-8")
    guard_pos = text.index("_PL_THIS_FILE")
    syspath_pos = text.index("sys.path.insert")
    astrbot_pos = text.index("from astrbot.api")
    assert guard_pos < syspath_pos < astrbot_pos
