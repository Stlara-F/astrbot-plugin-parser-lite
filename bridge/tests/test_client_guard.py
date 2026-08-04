"""DOWNLOAADER 客户端关闭检测 + 小黑盒评论 HTML 解析测试."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.core as core  # noqa: E402


class FakeClosedHttpx:
    is_closed = True


class FakeOpenHttpx:
    is_closed = False


class FakeClosedCurl:
    _curl = None


class FakeOpenCurl:
    _curl = object()


def test_client_closed_detects_httpx():
    """httpx is_closed 检测."""
    client = type("C", (), {"_httpx": FakeClosedHttpx(), "_curl": FakeOpenCurl()})()
    assert core._client_closed(client) is True
    client2 = type("C", (), {"_httpx": FakeOpenHttpx(), "_curl": FakeOpenCurl()})()
    assert core._client_closed(client2) is False


def test_client_closed_detects_curl():
    """curl_cffi 内部 _curl None (已关闭) 检测."""
    client = type("C", (), {"_httpx": FakeOpenHttpx(), "_curl": FakeClosedCurl()})()
    assert core._client_closed(client) is True


def test_client_closed_missing_session():
    """session 缺失 (未初始化) → closed."""
    client = type("C", (), {"_httpx": None, "_curl": None})()
    assert core._client_closed(client) is True


def test_ensure_parser_httpx_rebuilds_closed():
    """解析器 httpx 已关闭 (重载残留) → 请求前重建."""
    import asyncio

    from httpx import AsyncClient

    class FakeCls:
        __name__ = "FakeParserCls"

    class FakeParser:
        headers: dict = None  # type: ignore[assignment]
        timeout = 15
        httpx = None

    p = FakeParser()
    p.headers = {"User-Agent": "t"}
    p.httpx = AsyncClient(timeout=15)
    asyncio.run(p.httpx.aclose())
    assert p.httpx.is_closed is True

    pl = core.ParserLite()
    pl._parsers = {"FakeParserCls": p}
    pl._ensure_parser_httpx([FakeCls()])
    assert p.httpx.is_closed is False, "httpx 应被重建"


def test_ensure_parser_httpx_keeps_open():
    """httpx 正常 (未关闭) → 不重建 (同一实例)."""
    import asyncio

    from httpx import AsyncClient

    class FakeCls:
        __name__ = "FakeParserCls2"

    class FakeParser:
        headers = {}
        timeout = 15
        httpx = None

    p = FakeParser()
    p.httpx = AsyncClient(timeout=15)
    orig = p.httpx
    pl = core.ParserLite()
    pl._parsers = {"FakeParserCls2": p}
    pl._ensure_parser_httpx([FakeCls()])
    assert p.httpx is orig, "未关闭的 httpx 不应重建"
    asyncio.run(p.httpx.aclose())


def test_heybox_comment_html_to_text():
    """小黑盒评论 HTML → 纯文本 (解析器层)."""
    from nonebot_plugin_parser_lite.parsers.heybox.model import CommentItem

    class FakeUser:
        pass

    c = CommentItem(is_cy=0, create_at=0,
                    text='<a href="https://xiaoheihe.cn/user/1">@老哥</a>：<br>说的对',
                    ip_location="", child_num=0, up=0,
                    user=FakeUser(), imgs=[])
    items = c.content
    texts = [x for x in items if isinstance(x, str)]
    joined = "".join(texts)
    assert "@老哥" in joined
    assert "说的对" in joined
    assert "<a" not in joined
    assert "<br" not in joined
