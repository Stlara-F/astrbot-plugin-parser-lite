"""card_semantic 模块测试 — 与被测代码同目录 (bridge/tests/test_card_semantic.py)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.card_semantic import (  # noqa: E402
    find_json_cards,
    format_card_summary,
    inject_card_summary,
    parse_card,
    parse_link_share_card,
    parse_miniapp_card,
    parse_music_card,
)


def _miniapp():
    return {
        "app": "com.tencent.miniapp",
        "meta": {"detail_1": {"title": "小程序标题", "qqdocurl": "https://t.cn/A1"}},
    }


def _link_share():
    return {
        "app": "com.tencent.structmsg",
        "view": "news",
        "meta": {"news": {"title": "分享标题", "qqdocurl": "https://t.cn/A2"}},
    }


def _music():
    return {
        "app": "com.tencent.music",
        "view": "music",
        "meta": {"music": {"title": "歌名", "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/1"}},
    }


def test_parse_miniapp():
    card = parse_miniapp_card(_miniapp())
    assert card is not None
    assert card["title"] == "小程序标题"
    assert card["url"] == "https://t.cn/A1"


def test_parse_link_share():
    card = parse_link_share_card(_link_share())
    assert card is not None
    assert card["title"] == "分享标题"


def test_parse_music():
    card = parse_music_card(_music())
    assert card is not None
    assert card["kind"] == "音乐"


def test_parse_card_dispatch():
    assert parse_card(_miniapp())["kind"] == "小程序"
    assert parse_card(_link_share())["kind"] == "链接分享"
    assert parse_card(_music())["kind"] == "音乐"


def test_parse_card_string_input():
    card = parse_card(json.dumps(_link_share()))
    assert card is not None
    assert card["title"] == "分享标题"


def test_parse_card_invalid():
    assert parse_card(None) is None
    assert parse_card("not json") is None
    assert parse_card({"app": "unknown"}) is None


def test_format_card_summary():
    text = format_card_summary(parse_card(_link_share()))
    assert "[分享]" in text
    assert "分享标题" in text
    assert "https://t.cn/A2" in text


class _FakeCompJson:
    def __init__(self, data):
        self.data = data


class _FakeEvent:
    def __init__(self, chain):
        self._chain = chain
        self.message_obj = type("O", (), {"message": chain})()
        self.message_str = ""

    def get_messages(self):
        return self._chain

    def get_message_str(self):
        return self.message_str


def test_find_json_cards():
    ev = _FakeEvent([_FakeCompJson(_miniapp())])
    found = find_json_cards(ev)
    assert len(found) == 1
    assert found[0]["card"]["title"] == "小程序标题"


def test_inject_card_summary():
    ev = _FakeEvent([])
    card = parse_card(_link_share())
    inject_card_summary(ev, card)
    assert "[分享]" in ev.message_str
    assert "分享标题" in ev.message_str
