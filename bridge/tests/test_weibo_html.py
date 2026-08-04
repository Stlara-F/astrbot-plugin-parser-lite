"""微博 HTML 解析根因修复测试 — 评论 HTML 不再进 content.

回归保护: 曾把 API 原始 HTML (a/br/span class) 直接放进 content,
渲染图片显示标签文本. 现解析器层 BeautifulSoup 正确解析.
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nonebot_plugin_parser_lite.parsers.weibo.article_comment import (
    ArticleComment,  # noqa: E402
)


def test_weibo_article_comment_html_to_text():
    """微博文章评论: HTML text → 纯文本 content (无标签残留)."""
    html = ('<a href="https://weibo.com/u/123" rel="nofollow">@测试用户</a>：'
            '<br>今天天气<span class="url-icon"><img src="x"></span>不错')
    c = ArticleComment(
        created_at_unix=0, text=html,
        user_info={"id": 1, "screen_name": "u", "description": "", "profile_image_url": ""},
    )
    items = c.content
    texts = [x for x in items if isinstance(x, str)]
    joined = "".join(texts)
    assert texts
    assert "<a" not in joined
    assert "url-icon" not in joined
    assert "<br" not in joined
    assert "@测试用户" in joined
    assert "今天天气" in joined


def test_weibo_placeholder_preserved():
    """贴纸占位符 [xx] 保留供替换 (不被 HTML 解析吃掉)."""
    html = "正文[浪]哈哈"
    c = ArticleComment(
        created_at_unix=0, text=html,
        user_info={"id": 1, "screen_name": "u", "description": "", "profile_image_url": ""},
    )
    items = c.content
    stickers = [x for x in items if type(x).__name__ == "StickerContent"]
    assert stickers, "占位符应替换为贴纸"
