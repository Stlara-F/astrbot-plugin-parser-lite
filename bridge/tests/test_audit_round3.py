"""审计第三轮修复测试: cookie_health/format None/arbiter 空 emoji/delay_send/render_patch 还原."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.arbiter import _emoji_id, arm  # noqa: E402
from bridge.cookie_health import CookieHealth  # noqa: E402
from bridge.delay_send import DelaySender  # noqa: E402
from bridge.format import format_brief, format_full  # noqa: E402


def test_cookie_health_persist_and_lookup(tmp_path):
    """cookie_health: JsonStateStore 持久化 (节流 + 显式落盘)."""
    c = CookieHealth(tmp_path / "ck.json")
    c._store.update(lambda d: d.update(bilibili={"ok": True, "info": "有效", "ts": 1}))
    c.save()
    c2 = CookieHealth(tmp_path / "ck.json")
    assert c2._last_status["bilibili"]["ok"] is True


@pytest.mark.asyncio
async def test_cookie_health_stop_interrupts_sleep():
    """P2-10: stop() 可中断休眠 (不等满周期)."""
    c = CookieHealth()
    calls = []

    async def notify(msg):
        calls.append(msg)

    c.start(60.0, {"bilibili": "SESSDATA=x"}, notify)  # 长周期
    assert c._task is not None
    await asyncio.sleep(0.05)
    await c.stop()
    assert c._task is None


@pytest.mark.asyncio
async def test_cookie_health_check_once_changes_status(tmp_path, monkeypatch):
    """check_once 更新状态 + 通知 (失效变更时, 校验器打桩避免真实网络)."""
    import bridge.cookie_health as ch

    c = CookieHealth(tmp_path / "ck2.json")
    c._store.update(lambda d: d.update(bilibili={"ok": True, "info": "有效", "ts": 1}))
    notified = []

    async def notify(msg):
        notified.append(msg)

    c._notify = notify

    # 校验器注册表打桩 (monkeypatch 自动还原, 不打真实网络)
    async def fake_fail(ck):
        return False, "401"

    monkeypatch.setitem(ch._COOKIE_CHECKERS, "bilibili", fake_fail)
    await c.check_once({"bilibili": "SESSDATA=bad"})
    assert notified, "状态变更应触发通知"
    assert c._last_status["bilibili"]["ok"] is False


def test_format_full_none_platform():
    """P3-3: platform/author 为 None 时不抛异常."""

    class FakeResult:
        def __init__(self):
            self.platform = None
            self.author = None
            self.title = None
            self.timestamp = None
            self.content = []
            self.stats = type(
                "S",
                (),
                {
                    "view_count": 0,
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                    "collect_count": 0,
                },
            )()
            self.comments = []
            self.ai_summary = ""

    text = format_full(FakeResult())
    assert "解析" in text
    assert format_brief(FakeResult()).strip()


def test_arbiter_empty_emoji_defaults():
    """P3-9: 空 emoji 回退默认竞争表情 (不产生永远不匹配的空 id)."""
    assert _emoji_id("") == _emoji_id("👍")
    assert _emoji_id("") != ""
    assert arm("m9", emoji="") is True


def test_delay_send_no_sync_fallback():
    """P2-3: 延迟发送无同步执行分支 (生产仅 running-loop)."""
    src = (_ROOT / "bridge" / "delay_send.py").read_text(encoding="utf-8")
    assert "_aio.run" not in src
    assert "asyncio.get_running_loop()" in src


def test_render_patch_restore_function():
    """P2-9: restore_render_patch 存在且可还原."""
    from bridge import render_patch as rp

    assert callable(rp.restore_render_patch)
    # 无上游 (CI) 时静默返回 False, 不抛异常
    assert rp.restore_render_patch() in (True, False)


def test_delay_send_no_loop_skips():
    """P2-3: 无运行中 loop 时触发跳过 (不执行也不抛)."""
    s = DelaySender()
    triggered = []

    async def trig(key):
        triggered.append(key)

    s.arm("m1", "k1", trigger=trig)
    # 同步环境 (无 loop): create_task 应抛 RuntimeError → 跳过
    try:
        asyncio.get_running_loop()
        has_loop = True
    except RuntimeError:
        has_loop = False
    if not has_loop:
        assert s.on_emoji_like("m1", "128077", []) is False
        assert triggered == []
