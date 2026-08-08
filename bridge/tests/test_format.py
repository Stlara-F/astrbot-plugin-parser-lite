"""format 模块测试 — 与被测代码同目录 (bridge/tests/test_format.py)."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.send import format_brief, format_full  # noqa: E402
from nonebot_plugin_parser_lite.data import (  # noqa: E402
    Author,
    ParseResult,
    Platform,
    Stats,
    StickerContent,
)


def _make_result(*, with_comments=False, with_ai=False) -> ParseResult:
    content: list = ["正文文本"]
    stats = Stats(view_count=1000, like_count=50, comment_count=3)
    result = ParseResult(
        platform=Platform(name="bilibili", display_name="哔哩哔哩"),
        author=Author(name="测试作者"),
        title="测试标题",
        content=content,
        stats=stats,
        url="https://www.bilibili.com/video/BV1xx411c7mD",
    )
    if with_comments:
        result.comments = [
            type("C", (), {"author": Author(name="u1"), "content": ["评论一"]})(),
        ]
    if with_ai:
        result.ai_summary = "AI 摘要内容"
    return result


def test_format_full_basic():
    text = format_full(_make_result())
    assert "哔哩哔哩" in text
    assert "测试作者" in text
    assert "测试标题" in text
    assert "正文文本" in text
    assert "播放1000" in text
    assert "赞50" in text
    assert "评论3" in text


def test_format_full_comments_and_ai():
    text = format_full(_make_result(with_comments=True, with_ai=True))
    assert "--- 评论" in text
    assert "AI摘要" in text


def test_format_brief():
    text = format_brief(_make_result())
    assert "哔哩哔哩" in text
    assert "测试作者" in text
    assert "播放1000" in text
    assert "正文文本" not in text  # brief 不含正文


def test_format_full_sticker_desc():
    r = _make_result()
    r.content = [
        "text",
        StickerContent(desc="[sticker]", size="medium", path_task=object()),
    ]
    out = format_full(r)
    assert "text" in out
    assert "[sticker]" in out  # sticker desc shown in order
