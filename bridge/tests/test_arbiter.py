"""arbiter 模块测试 — 与被测代码同目录 (bridge/tests/test_arbiter.py)."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.arbiter import (  # noqa: E402
    arm,
    check_notice,
    concede,
    disarm,
    is_notice_event,
    parse_notice,
)


def _reset():
    import bridge.arbiter as a

    a._pending.clear()
    a._conceded.clear()


def test_arm_returns_true_first():
    _reset()
    assert arm("m1") is True


def test_arm_returns_false_after_concede():
    _reset()
    concede("m1")
    assert arm("m1") is False


def test_check_notice_concedes_on_match():
    _reset()
    arm("m1", emoji="👍")
    # 其他 bot 回应同一表情
    assert check_notice("m1", "128077") is True  # 👍 的码点 128077
    assert arm("m1") is False  # 已放弃


def test_check_notice_ignores_different_emoji():
    _reset()
    arm("m1", emoji="👍")
    assert check_notice("m1", "999999") is False  # 不同表情不放弃


def test_parse_notice_valid():
    raw = {
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "target_msg_id": 12345,
        "emoji_id": "128077",
    }
    assert parse_notice(raw) == ("12345", "128077")


def test_parse_notice_invalid():
    assert parse_notice({"post_type": "message"}) is None
    assert parse_notice(None) is None
    assert parse_notice({"post_type": "notice", "notice_type": "group_increase"}) is None


class _FakeEvent:
    def __init__(self, raw):
        self.raw_message = raw
        self.message_obj = type("O", (), {"raw_message": raw})()


def test_is_notice_event():
    assert is_notice_event(_FakeEvent({"post_type": "notice"})) is True
    assert is_notice_event(_FakeEvent({"post_type": "message"})) is False


def test_disarm_clears():
    _reset()
    concede("m1")
    disarm("m1")
    assert arm("m1") is True
