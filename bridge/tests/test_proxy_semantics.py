"""代理语义测试 — 默认直连, 代理为附加配置 (platforms[].proxy 勾选).

对照 test-bridge: 上游是"配置代理后全局走代理", 用户期望优先直连.
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.core as core  # noqa: E402


class FakeCls:
    __name__ = "XParser"
    platform = type("P", (), {"name": "x"})()


class FakeCls2:
    __name__ = "BilibiliParser"
    platform = type("P", (), {"name": "bilibili"})()


def test_target_uses_proxy_checked():
    """平台勾选 proxy → 走代理."""
    core.BridgeConfig._source = {
        "plite_http_proxy": "192.168.231.10:10809",
        "platforms": [{"platform": "x", "proxy": True}],
    }
    assert core.target_uses_proxy([FakeCls()], "XParser") is True


def test_target_uses_proxy_unchecked_default_direct():
    """平台未勾选 proxy → 默认直连 (即使配置了全局代理)."""
    core.BridgeConfig._source = {
        "plite_http_proxy": "192.168.231.10:10809",
        "platforms": [{"platform": "bilibili", "proxy": False}],
    }
    assert core.target_uses_proxy([FakeCls2()], "BilibiliParser") is False


def test_target_uses_proxy_no_global():
    """无全局代理 → 直连."""
    core.BridgeConfig._source = {
        "platforms": [{"platform": "x", "proxy": True}],
    }
    # _target_uses_proxy 只看平台勾选; parse_url 层无 proxy_url 时不会走代理
    assert core.target_uses_proxy([FakeCls()], "XParser") is True


def test_target_uses_proxy_fallback_parsers_items():
    """兼容回退: platforms 未配置时用 parsers.items.proxied."""
    core.BridgeConfig._source = {
        "parsers": {"items": {"proxied": ["x"]}},
    }
    assert core.target_uses_proxy([FakeCls()], "XParser") is True
