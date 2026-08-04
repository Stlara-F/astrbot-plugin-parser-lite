"""delay_send 模块测试 — 与被测代码同目录 (bridge/tests/test_delay_send.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.delay_send import DelaySender, make_delay_sender  # noqa: E402


def _sender():
    return DelaySender()


def test_should_delay_threshold():
    s = _sender()
    assert s.should_delay(30 * 1024 * 1024, 20 * 1024 * 1024) is True
    assert s.should_delay(10 * 1024 * 1024, 20 * 1024 * 1024) is False


@pytest.mark.asyncio
async def test_arm_and_trigger():
    s = _sender()
    triggered = []

    async def trig(key):
        triggered.append(key)

    s.arm("m1", "k1", trigger=trig)
    assert s.pending_count() == 1
    assert s.on_emoji_like("m1", "128077", []) is True
    assert s.pending_count() == 0
    await asyncio.sleep(0)
    assert triggered == ["k1"]


@pytest.mark.asyncio
async def test_emoji_filter():
    s = _sender()
    triggered = []

    async def trig(key):
        triggered.append(key)

    s.arm("m1", "k1", trigger=trig)
    # 表情不匹配 → 不触发, pending 保留
    assert s.on_emoji_like("m1", "999999", ["128077"]) is False
    assert s.pending_count() == 1
    # 匹配 → 触发
    assert s.on_emoji_like("m1", "128077", ["128077"]) is True
    await asyncio.sleep(0)
    assert triggered == ["k1"]


def test_unknown_msg_id():
    s = _sender()
    assert s.on_emoji_like("nope", "128077", []) is False


def test_cleanup_expired():
    s = _sender()
    s.arm("m1", "k1", timeout_sec=-1)  # 立即过期
    assert s.cleanup() == 1
    assert s.pending_count() == 0


def test_make_delay_sender():
    assert make_delay_sender() is not None
