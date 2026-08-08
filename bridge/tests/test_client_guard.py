"""DOWNLOADER 客户端关闭检测 + 小黑盒评论 HTML 解析测试 (r8: _ensure_parser_httpx 已删)."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.adapter as adapter  # noqa: E402


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
    assert adapter.client_closed(client) is True
    client2 = type("C", (), {"_httpx": FakeOpenHttpx(), "_curl": FakeOpenCurl()})()
    assert adapter.client_closed(client2) is False


def test_client_closed_detects_curl():
    """curl_cffi 内部 _curl None (已关闭) 检测."""
    client = type("C", (), {"_httpx": FakeOpenHttpx(), "_curl": FakeClosedCurl()})()
    assert adapter.client_closed(client) is True


def test_client_closed_missing_session():
    """session 缺失 (未初始化) → closed."""
    client = type("C", (), {"_httpx": None, "_curl": None})()
    assert adapter.client_closed(client) is True


def test_parserlite_no_self_managed_parsers():
    """r8: ParserLite 不再自研解析器实例管理 (委托上游 pipeline.Parser)."""
    import inspect

    from bridge.pipeline import ParserLite

    src = inspect.getsource(ParserLite)
    assert "_parsers" not in src
    assert "_ensure_parser_httpx" not in src
    assert "_try_all_parsers" not in src
    assert "UpParser" in src or "pipeline" in src
