"""命令业务委托测试 (r11: commands.py 全委托, FakeEvent 注入).

覆盖: parse/bm/blogin/install_chromium/doctor/parse_url_cmd + 判定辅助.
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import types

import pytest


@pytest.fixture(autouse=True)
def _astrbot_stub(monkeypatch):
    """CI 无 astrbot 环境 → stub astrbot.api.message_components."""
    comp = types.ModuleType("astrbot.api.message_components")

    class Image:
        @staticmethod
        def fromBytes(raw):
            return ("img", raw)

    class Plain:
        def __init__(self, text):
            self.text = text

    comp.Image = Image
    comp.Plain = Plain
    api = types.ModuleType("astrbot.api")
    api.message_components = comp
    ast = types.ModuleType("astrbot")
    ast.api = api
    monkeypatch.setitem(sys.modules, "astrbot", ast)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", comp)


class FakeEvent:
    def __init__(self, text="", sender="u1"):
        self._text = text
        self._sender = sender
        self.sent = []
        self.unified_msg_origin = "group:12345"
        self.message_obj = None

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, segs):
        return ("chain", segs)


class FakePlugin:
    def __init__(self):
        self._disabled_groups = set()
        self._parser = None

    async def _parse_raw(self, url):
        from nonebot_plugin_parser_lite.data import ParseResult

        return ParseResult(url=url, platform=None, author=None, title=url, content=[])

    async def _parse_and_format(self, url):
        return f"formatted:{url}"


@pytest.mark.asyncio
async def test_parse_disabled(monkeypatch):
    from bridge import commands

    plugin = FakePlugin()
    plugin._disabled_groups.add("12345")
    event = FakeEvent(text="https://bilibili.com/video/BV1x")
    msgs = [m async for m in commands.parse(plugin, event)]
    assert msgs[0][1] == "本群已禁用"


@pytest.mark.asyncio
async def test_parse_no_url(monkeypatch):
    from bridge import commands

    plugin = FakePlugin()
    event = FakeEvent(text="没有链接")
    msgs = [m async for m in commands.parse(plugin, event)]
    assert msgs[0][1] == "未找到链接"


@pytest.mark.asyncio
async def test_parse_success_dispatch(monkeypatch):
    from bridge import commands

    dispatched = []

    async def fake_dispatch(event, result):
        dispatched.append(result)

    monkeypatch.setattr(
        "bridge.commands._extract_urls", lambda e: ["https://bilibili.com/video/BV1x"]
    )
    monkeypatch.setattr("bridge.send.dispatch_result", fake_dispatch)
    plugin = FakePlugin()
    event = FakeEvent(text="https://bilibili.com/video/BV1x")
    msgs = [m async for m in commands.parse(plugin, event)]
    assert len(dispatched) == 1
    assert msgs == []


@pytest.mark.asyncio
async def test_bm_audio(monkeypatch):
    from bridge import commands

    class FakeBili:
        def __init__(self):
            self.closed = False

        async def extract_download_urls(self, bvid=None):
            return ["http://v/1", "http://a/1"]

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        "nonebot_plugin_parser_lite.parsers.bilibili.BilibiliParser", lambda: FakeBili()
    )
    plugin = FakePlugin()
    event = FakeEvent(text="https://b23.tv/BV1xx411c7mD")
    msgs = [m async for m in commands.bm(plugin, event)]
    assert msgs[0][1].startswith("Audio:")


@pytest.mark.asyncio
async def test_blogin_qrcode(monkeypatch):
    from bridge import commands

    class FakeBili:
        async def login_with_qrcode(self):
            return b"\x89PNG"

    monkeypatch.setattr(
        "nonebot_plugin_parser_lite.parsers.bilibili.BilibiliParser", lambda: FakeBili()
    )
    plugin = FakePlugin()
    event = FakeEvent()
    msgs = [m async for m in commands.blogin(plugin, event)]
    assert msgs[0][1].startswith("B站登录二维码")
    assert msgs[1][0] == "chain"


@pytest.mark.asyncio
async def test_install_chromium_delegates(monkeypatch):
    from bridge import commands

    async def fake_ensure(browsers_path="", started_msg="ok"):
        return True, [started_msg, "已安装"]

    monkeypatch.setattr("bridge.commands.ensure_chromium", fake_ensure)
    plugin = FakePlugin()
    event = FakeEvent()
    msgs = [m async for m in commands.install_chromium(plugin, event)]
    assert msgs[0][1] == "Chromium 已可用, 无需重复安装"


@pytest.mark.asyncio
async def test_doctor_ok(monkeypatch):
    from bridge import commands

    async def fake_checks():
        return []

    def fake_summarize(results):
        return {"failed": 0, "warn": 0}

    def fake_render(results, summary):
        return "OK report"

    monkeypatch.setattr("bridge.commands.run_checks", fake_checks)
    monkeypatch.setattr("bridge.commands.summarize", fake_summarize)
    monkeypatch.setattr("bridge.commands.render_text", fake_render)
    plugin = FakePlugin()
    event = FakeEvent()
    msgs = [m async for m in commands.doctor(plugin, event)]
    assert msgs[0][1] == "OK report"


@pytest.mark.asyncio
async def test_parse_url_cmd(monkeypatch):
    from bridge import commands

    plugin = FakePlugin()
    event = FakeEvent(sender="black")
    # 黑名单判定: sender 不在 blacklist_users → 正常解析
    out = await commands.parse_url_cmd(plugin, event, "https://bilibili.com/video/BV1x")
    assert out == "formatted:https://bilibili.com/video/BV1x"


def test_gid_and_disabled():
    from bridge import commands

    event = FakeEvent()
    assert commands.gid(event) == "12345"
    plugin = FakePlugin()
    assert commands.is_disabled(plugin, event) is False
    plugin._disabled_groups.add("12345")
    assert commands.is_disabled(plugin, event) is True
