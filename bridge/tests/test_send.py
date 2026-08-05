"""发送层测试 — 上游渲染 → AstrBot 发送的解耦边界."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.send as send  # noqa: E402
from bridge.context import BridgeConfig  # noqa: E402


def test_sendable_types_dynamic():
    """发送类型从上游 ContentItem Union 动态扫描 (0 hardcode)."""
    types = send.get_sendable_types()
    assert "card" in types
    assert "image" in types
    assert "video" in types
    assert "audio" in types


def test_should_send_default_all():
    """未配置 send_strategy → 默认全部类型."""
    BridgeConfig._source = {}
    assert send.should_send("card") is True
    assert send.should_send("video") is True


def test_should_send_configured():
    """配置 send_strategy 后按列表门控."""
    BridgeConfig._source = {"send_strategy": ["card"]}
    assert send.should_send("card") is True
    assert send.should_send("video") is False


def test_should_send_json_string():
    """send_strategy 为 JSON 字符串时兼容解析."""
    BridgeConfig._source = {"send_strategy": '["card","image"]'}
    assert send.should_send("image") is True
    assert send.should_send("audio") is False


class FakeEvent:
    def __init__(self):
        self.sent = []

    def chain_result(self, segs):
        return segs

    async def send(self, segs):
        self.sent.append(segs)


def test_send_card_fallback_text():
    """渲染失败 → 回退纯文本 (发送仍成功)."""

    async def fake_render(result):
        raise RuntimeError("render boom")

    orig = send.up_renderer
    try:
        send.up_renderer = lambda: type("R", (), {"render_image": fake_render})()
        ev = FakeEvent()
        import asyncio

        ok = asyncio.run(send.send_card(
            ev, type("R", (), {"url": "https://x"}), lambda r: "回退文本"))
        assert ok is True
        assert len(ev.sent) == 1
    finally:
        send.up_renderer = orig
