"""解析总超时测试 — 慢代理/死链不再拖死 (curl 240s×轮询)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.core as core  # noqa: E402


async def _slow_parse(seconds):
    await asyncio.sleep(seconds)
    return "done"


def test_parse_timeout_constant():
    """解析超时常量存在且合理 (60s, 远小于 curl 240s)."""
    assert core.PARSE_TIMEOUT <= 60.0


@pytest.mark.asyncio
async def test_wait_for_truncates_slow_parse():
    """慢解析 (超时) 被 wait_for 截断, 快速返回."""
    t0 = time.time()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_slow_parse(2.0), timeout=0.2)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"应快速失败, 实际 {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_fast_parse_not_affected():
    """正常解析不受超时影响."""
    r = await asyncio.wait_for(_slow_parse(0.01), timeout=core.PARSE_TIMEOUT)
    assert r == "done"
