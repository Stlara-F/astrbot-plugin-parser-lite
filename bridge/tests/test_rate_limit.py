"""rate_limit 模块测试 — 与被测代码同目录 (bridge/tests/test_rate_limit.py)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.rate_limit import RateLimiter, clean_url, load_rate_cfg  # noqa: E402


def _limiter():
    tmp = tempfile.mkdtemp()
    return RateLimiter(os.path.join(tmp, "rl.json"))


def test_allow_under_limit():
    rl = _limiter()
    cfg = {"enabled": True, "max_per_window": 2, "window_seconds": 60}
    ok1, _ = rl.allow(url="https://a.com/x", cfg=cfg)
    ok2, _ = rl.allow(url="https://a.com/x", cfg=cfg)
    assert ok1
    assert ok2


def test_allow_over_limit():
    rl = _limiter()
    cfg = {"enabled": True, "max_per_window": 2, "window_seconds": 60}
    rl.allow(url="https://a.com/x", cfg=cfg)
    rl.allow(url="https://a.com/x", cfg=cfg)
    ok, why = rl.allow(url="https://a.com/x", cfg=cfg)
    assert not ok
    assert "过于频繁" in why


def test_disabled_always_allowed():
    rl = _limiter()
    cfg = {"enabled": False}
    for _ in range(10):
        ok, _ = rl.allow(url="https://a.com/x", cfg=cfg)
        assert ok


def test_user_limit():
    rl = _limiter()
    cfg = {"enabled": True, "max_per_window": 99, "max_per_user_window": 2, "window_seconds": 60}
    assert rl.allow(url="https://a.com/1", user_id="u1", cfg=cfg)[0]
    assert rl.allow(url="https://a.com/2", user_id="u1", cfg=cfg)[0]
    ok, why = rl.allow(url="https://a.com/3", user_id="u1", cfg=cfg)
    assert not ok
    assert "频率超限" in why


def test_clean_url_strips_tracking():
    cleaned = clean_url("https://x.com/a?utm_source=ig&id=1&share_token=abc")
    assert "utm_source" not in cleaned
    assert "share_token" not in cleaned
    assert "id=1" in cleaned


def test_clean_url_preserves_bare():
    assert clean_url("https://b23.tv/abc") == "https://b23.tv/abc"


def test_load_rate_cfg_variants():
    assert load_rate_cfg(None) == {}
    assert load_rate_cfg({"rate_limit": {"enabled": True}})["enabled"] is True
    assert load_rate_cfg({"rate_limit": '{"enabled": false}'})["enabled"] is False
    assert load_rate_cfg({"rate_limit": "bad-json"}) == {}
