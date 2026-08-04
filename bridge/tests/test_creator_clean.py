"""通用管道清洗测试 — 评论/正文 HTML 在 Creator 入口统一解析.

根因修复: bilibili/rednote/heybox 等平台评论 message 含 HTML
(<a class=bili-at>/<br>/<span class>), 在 Creator.comment/BaseParser.result
数据入口统一 BeautifulSoup 解析, 覆盖所有解析器 (非逐个打补丁).
"""

from __future__ import annotations

from pathlib import Path
import sys

from nonebot_plugin_parser_lite.creator import Creator  # noqa: E402
from nonebot_plugin_parser_lite.data import Author  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_clean_html_in_items_bilibili_at():
    """B站评论 @提及 HTML → 纯文本."""
    items = ['<a href="//space.bilibili.com/1" class="bili-at">@老哥</a>：<br>评论内容',
             "纯文本", 123]
    out = Creator._clean_html_in_items(items)
    assert "@老哥" in out[0]
    assert "评论内容" in out[0]
    assert "bili-at" not in out[0]
    assert "<a" not in out[0]
    assert out[1] == "纯文本"
    assert out[2] == 123


def test_clean_html_in_items_span_img():
    """评论含 span/img 标签 → 清除."""
    out = Creator._clean_html_in_items(
        ['<span class="url-icon"><img src="x"></span>正文'])
    assert "正文" in out[0]
    assert "url-icon" not in out[0]
    assert "<img" not in out[0]


def test_comment_factory_cleans_html():
    """Creator.comment 构造时自动清洗 (所有解析器经此入口)."""
    c = Creator.comment(
        author=Author(name="u", id="1"),
        content=["<a href='//x' class='bili-at'>@用户</a>内容"],
        timestamp=0,
    )
    assert "@用户" in c.content[0]
    assert "bili-at" not in c.content[0]


def test_result_factory_cleans_html():
    """BaseParser.result 构造时自动清洗主 content."""
    from nonebot_plugin_parser_lite.parsers.base import BaseParser

    class P(BaseParser):
        platform = type("PL", (), {"display_name": "t", "name": "t"})()

    r = P.result(author=Author(name="u", id="1"), url="https://x",
                 content=["<p>正文<b>粗</b></p>"])
    assert "正文" in r.content[0]
    assert "<p" not in r.content[0]


def test_clean_keeps_sticker_placeholder():
    """贴纸占位符 [xx] 在清洗后保留 (供替换)."""
    out = Creator._clean_html_in_items(["正文[浪]哈哈"])
    assert "[浪]" in out[0]
