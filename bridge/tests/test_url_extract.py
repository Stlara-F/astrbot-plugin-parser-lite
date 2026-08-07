"""url_extract 模块测试 — 与被测代码同目录 (bridge/tests/test_url_extract.py)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.url_extract import (  # noqa: E402
    collect_urls,
    extract_card_json_url,
    extract_forward_urls,
    extract_json_urls,
    extract_urls,
    extract_xml_urls,
)


class _FakeComp:
    """模拟 astrbot.api.message_components 的 isinstance 目标."""

    class Json:
        def __init__(self, data):
            self.data = data

    class Image:
        pass


class _FakeEvent:
    def __init__(self, chain=None, text=""):
        self._chain = chain or []
        self._text = text
        self.message_obj = type("O", (), {"message": self._chain})()

    def get_messages(self):
        return self._chain

    def get_message_str(self):
        return self._text


def test_collect_urls_basic():
    urls = []
    collect_urls("看这个 https://a.com/x 和 https://b.com/y", urls)
    assert urls == ["https://a.com/x", "https://b.com/y"]


def test_extract_card_json_url_named_key():
    data = {"meta": {"news": {"qqdocurl": "https://t.cn/A123"}}}
    assert extract_card_json_url(data) == "https://t.cn/A123"


def test_extract_card_json_url_skip_assets():
    data = {"logo": "https://cdn.x.com/logo.png"}
    assert extract_card_json_url(data) is None


def test_extract_json_urls():
    urls = []
    extract_json_urls(json.dumps({"jumpUrl": "https://a.com/1"}), urls)
    assert "https://a.com/1" in urls


def test_extract_xml_urls():
    urls = []
    extract_xml_urls("<msg><url>https://a.com/2</url></msg>", urls)
    assert "https://a.com/2" in urls


def test_extract_forward_urls():
    seg = {
        "messages": [
            {"message": [{"type": "text", "data": {"text": "内嵌 https://a.com/3"}}]},
        ]
    }
    urls = []
    extract_forward_urls(seg, urls)
    assert "https://a.com/3" in urls


def test_extract_urls_from_json_comp():
    event = _FakeEvent(
        chain=[_FakeComp.Json({"meta": {"news": {"qqdocurl": "https://t.cn/B1"}}})],
        text="",
    )
    urls = extract_urls(event, _FakeComp)
    assert "https://t.cn/B1" in urls


def test_extract_urls_from_plain_text():
    event = _FakeEvent(text="看 https://b23.tv/abc 不错")
    urls = extract_urls(event, _FakeComp)
    assert "https://b23.tv/abc" in urls


def test_extract_urls_dedup_and_clean():
    event = _FakeEvent(text="https://a.com/x 和 https://a.com/x")
    urls = extract_urls(event, _FakeComp)
    assert urls == ["https://a.com/x"]


def test_extract_urls_strips_trailing_punct():
    event = _FakeEvent(text="点这里 https://a.com/x。")
    urls = extract_urls(event, _FakeComp)
    assert urls == ["https://a.com/x"]
