"""CustomParser 功能测试 (回归保护: 自定义解析器可用性)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.core as core  # noqa: E402


def _sample_entry():
    return {
        "name": "test_parser",
        "display": "测试解析器",
        "url_pattern": r"https?://example\.com/.+",
        # 注意: 解析器先剥 HTML 标签, 正则应匹配纯文本 (无空格分隔)
        "title_re": r"标题[:：](.*?)作者",
        "author_re": r"作者[:：](.*?)正文",
        "text_re": r"正文[:：](.+)",
    }


def test_custom_parser_init():
    cp = core.CustomParser(_sample_entry())
    assert cp._name == "test_parser"
    assert cp._display == "测试解析器"
    assert cp._url_re is not None


def test_custom_parser_search_url():
    cp = core.CustomParser(_sample_entry())
    kw, m = cp.search_url("https://example.com/post/123")
    assert kw == "https://example.com/post/123"
    assert m is not None
    kw2, _m2 = cp.search_url("https://other.com/x")
    assert kw2 is None


def test_custom_parser_parse():
    cp = core.CustomParser(_sample_entry())
    html = "<html><title>标题：测试标题</title><p>作者：测试作者</p><p>正文：正文内容</p></html>"
    import httpx

    # 用 MockTransport 回放, 无全局 monkeypatch
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text=html))

    orig_client = httpx.AsyncClient

    async def run():
        class _FakeAsyncClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw.pop("headers", None)
                kw.pop("follow_redirects", None)
                super().__init__(*a, transport=transport, **kw)

        try:
            httpx.AsyncClient = _FakeAsyncClient  # type: ignore[misc]
            result = await cp.parse("https://example.com/post/123", "match")
            assert result is not None
            assert result.title == "测试标题"
            assert result.url == "https://example.com/post/123"
        finally:
            httpx.AsyncClient = orig_client  # type: ignore[misc]

    asyncio.run(run())


def test_custom_parser_schema_exists():
    """CustomParser.SCHEMA 应有必要字段 (注入 template_list 用)."""
    keys = [s["key"] for s in core.CustomParser.SCHEMA]
    for required in ("name", "url_pattern", "title_re"):
        assert required in keys
