"""功能模块独立测试 — 配置源注入, 无全局 BridgeConfig 依赖.

每个功能模块自包含 (load_cfg 注入配置源) + 独立运行 (无 astrbot/上游).
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.cfg as cfg  # noqa: E402


def test_module_cfg_extracts_section():
    """module_cfg: 从配置源提取模块段 (注入模式)."""
    src = {"delay_send": {"enabled": True}, "push": [{"uid": "1"}]}
    assert cfg.module_cfg(src, "delay_send") == {"enabled": True}
    assert cfg.module_cfg(src, "push") == [{"uid": "1"}]
    assert cfg.module_cfg(src, "arbiter", {}) == {}
    assert cfg.module_cfg(None, "x", 42) == 42


def test_module_cfg_json_string():
    """模块段为 JSON 字符串时兼容解析."""
    assert cfg.module_cfg({"push": '[{"uid":"1"}]'}, "push") == [{"uid": "1"}]
    assert cfg.module_cfg({"push": "not-json"}, "push", []) == []


def test_delay_send_cfg_injected():
    """delay_send: 注入配置源独立运行 (无全局依赖)."""
    from bridge.delay_send import load_cfg

    c = load_cfg({"delay_send": {"enabled": True, "threshold_mb": 50}})
    assert c["enabled"] is True
    assert c["threshold_mb"] == 50
    assert c["timeout_sec"] == 300.0
    c2 = load_cfg({})
    assert c2["enabled"] is False


def test_arbiter_cfg_injected():
    """arbiter: 注入配置源独立运行."""
    from bridge.arbiter import load_cfg

    c = load_cfg({"arbiter": {"enabled": True, "emoji": "❤", "window_sec": 2.0}})
    assert c["enabled"] is True
    assert c["emoji"] == "❤"
    assert c["window_sec"] == 2.0
    assert load_cfg({})["enabled"] is False


def test_cookie_health_cfg_injected():
    """cookie_health: 注入配置源独立运行."""
    from bridge.cookie_health import load_cfg

    c = load_cfg({"cookie_health": {"enabled": True, "interval_sec": 7200}})
    assert c["enabled"] is True
    assert c["interval_sec"] == 7200


def test_push_cfg_injected():
    """push: 注入配置源独立运行 (订阅 + 间隔)."""
    from bridge.push import load_cfg

    subs, interval = load_cfg(
        {
            "push": [{"uid": "1", "groups": "1,2", "enabled": True}],
            "push_interval": 120,
        }
    )
    assert subs == [{"uid": "1", "groups": "1,2", "enabled": True}]
    assert interval == 120
    # 旧 dict 格式兼容
    subs2, _ = load_cfg({"push": {"10001": ["1", "2"]}})
    assert subs2[0]["uid"] == "10001"
    assert subs2[0]["groups"] == "1,2"


def test_debounce_cfg_injected():
    """debounce: 注入配置源独立运行 (0 值合法)."""
    from bridge.debounce import load_cfg

    assert load_cfg({"plite_dedup_ttl": 0})["ttl_sec"] == 0.0
    assert load_cfg({"plite_dedup_ttl": 30})["ttl_sec"] == 30.0
    assert load_cfg({})["ttl_sec"] == 60.0


def test_rate_limit_cfg_injected():
    """rate_limit: 注入配置源独立运行 (已有模式)."""
    from bridge.rate_limit import load_rate_cfg

    c = load_rate_cfg({"rate_limit": {"enabled": True, "per_min": 5}})
    assert c["enabled"] is True
    assert load_rate_cfg(None) == {}
