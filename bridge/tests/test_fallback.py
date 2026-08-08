"""fallback 模块测试 — 与被测代码同目录 (bridge/tests/test_fallback.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.send import safe, send_with_fallback, truncate_text  # noqa: E402


def test_safe_returns_none_on_error():
    @safe(label="test")
    async def boom():
        raise ValueError("x")

    assert asyncio.run(boom()) is None


def test_safe_returns_value():
    @safe(label="test")
    async def ok():
        return 42

    assert asyncio.run(ok()) == 42


def test_safe_reraises_cancelled():
    @safe(label="test")
    async def cancel():
        raise asyncio.CancelledError()

    try:
        asyncio.run(cancel())
        pytest.fail("应抛出 CancelledError")
    except asyncio.CancelledError:
        pass


def test_send_with_fallback_first_succeeds():
    async def t1():
        return True

    async def t2():
        return False

    assert asyncio.run(send_with_fallback(try_send=t1, fallbacks=[t2])) is True


def test_send_with_fallback_second_succeeds():
    async def t1():
        return False

    async def t2():
        return True

    assert asyncio.run(send_with_fallback(try_send=t1, fallbacks=[t2])) is True


def test_send_with_fallback_all_fail():
    async def t1():
        return False

    async def t2():
        return False

    assert asyncio.run(send_with_fallback(try_send=t1, fallbacks=[t2])) is False


def test_send_with_fallback_exception_continues():
    async def t1():
        raise RuntimeError("boom")

    async def t2():
        return True

    assert asyncio.run(send_with_fallback(try_send=t1, fallbacks=[t2])) is True


def test_truncate_text():
    assert truncate_text("hello", 10) == "hello"
    assert truncate_text("hello world", 8) == "hello w…"
    assert truncate_text("x", 0) == "x"
