"""功能模块独立测试 — 配置源注入, 无全局 BridgeConfig 依赖.

每个功能模块自包含 (load_cfg 注入配置源) + 独立运行 (无 astrbot/上游).
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.config as cfg  # noqa: E402


def test_module_cfg_extracts_section():
    """module_cfg: 从配置源提取模块段 (注入模式)."""
    src = {"custom": {"enabled": True}, "items": [{"uid": "1"}]}
    assert cfg.module_cfg(src, "custom") == {"enabled": True}
    assert cfg.module_cfg(src, "items") == [{"uid": "1"}]
    assert cfg.module_cfg(src, "missing", {}) == {}
    assert cfg.module_cfg(None, "x", 42) == 42


def test_module_cfg_json_string():
    """模块段为 JSON 字符串时兼容解析."""
    assert cfg.module_cfg({"items": '[{"uid":"1"}]'}, "items") == [{"uid": "1"}]
    assert cfg.module_cfg({"items": "not-json"}, "items", []) == []


def test_removed_modules_cfg():
    """T3: delay_send/arbiter/cookie_health 已移除 (配置段不再存在)."""
    import importlib
    import sys

    for mod in ("bridge.delay_send", "bridge.arbiter", "bridge.cookie_health"):
        sys.modules.pop(mod, None)
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)
