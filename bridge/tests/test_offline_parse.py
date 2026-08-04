"""离线解析测试 — fixture 回放, 不触网 (E3).

前置: test/fixtures/ 存在录制文件 (由 run_local.py --record 生成).
无 fixture 时自动 skip — CI 无网络也可跑.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_FIXTURE_DIR = _ROOT / "test" / "fixtures"


def _has_fixtures() -> bool:
    return _FIXTURE_DIR.exists() and any(_FIXTURE_DIR.glob("*.json"))


pytestmark = pytest.mark.skipif(
    not _has_fixtures(), reason="fixtures 未录制 (先运行: PARSER_LITE_RECORD_DIR=test/fixtures python run_local.py <url>)"
)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _run_parse(url: str):
    from bridge.core import BridgeConfig, ParserLite
    from bridge.fixtures import patch_httpx_send

    # 隔离: 重置共享配置, 避免其他测试污染 (platforms enable=False 等)
    BridgeConfig._source = {}
    BridgeConfig._instance = None
    BridgeConfig._hash = ""
    patch_httpx_send(replay=True)
    os.environ.setdefault("PARSER_LITE_STANDALONE", "1")
    os.environ.setdefault("PARSER_LITE_BASE_DIR", str(_ROOT / ".parser-lite-test"))

    p = ParserLite()
    try:
        return asyncio.run(p.parse_url(url))
    finally:
        asyncio.run(p.close())


def test_offline_bilibili_parse():
    """回放 B站视频 fixture, 断言解析结构 (0 硬编码: 从 fixture 的 url 字段动态推导 BV 号)."""
    fx_files = sorted(_FIXTURE_DIR.glob("*.json"))
    assert fx_files, "fixtures 为空"
    import json
    import re
    # 从任一 fixture 的 url 字段提取 bvid → 构造分享链接
    bvid = None
    for f in fx_files:
        data = json.loads(f.read_text("utf-8"))
        m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", data.get("url", ""))
        if m:
            bvid = m.group(0)
            break
    if bvid is None:
        pytest.skip("fixtures 无 bvid, 先录制: PARSER_LITE_RECORD_DIR=test/fixtures python run_local.py <b站视频链接>")

    url = f"https://www.bilibili.com/video/{bvid}"
    result = _run_parse(url)
    assert result is not None
    assert result.platform is not None
    assert result.title, "标题为空"
    assert result.content, "内容为空"
