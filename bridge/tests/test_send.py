"""发送层测试 — 上游渲染 → AstrBot 发送的解耦边界."""

from __future__ import annotations

from pathlib import Path
import sys

from _pytest.monkeypatch import MonkeyPatch
import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.context import BridgeConfig  # noqa: E402
import bridge.send as send  # noqa: E402


def test_sendable_types_dynamic():
    """发送类型从上游 ContentItem Union 动态扫描 (0 hardcode)."""
    types = send.get_sendable_types()
    assert "card" in types
    assert "image" in types
    assert "video" in types
    assert "audio" in types


def test_should_send_default_all():
    """未配置 send_strategy → 默认全部类型."""
    BridgeConfig._source = {}
    assert send.should_send("card") is True
    assert send.should_send("video") is True


def test_should_send_configured():
    """配置 send_strategy 后按列表门控."""
    BridgeConfig._source = {"send_strategy": ["card"]}
    assert send.should_send("card") is True
    assert send.should_send("video") is False


def test_should_send_json_string():
    """send_strategy 为 JSON 字符串时兼容解析."""
    BridgeConfig._source = {"send_strategy": '["card","image"]'}
    assert send.should_send("image") is True
    assert send.should_send("audio") is False


class FakeEvent:
    def __init__(self):
        self.sent = []

    def chain_result(self, segs):
        return segs

    async def send(self, segs):
        self.sent.append(segs)


class FakeCmp:
    """假组件 (避免测试触发 AstrBot sqlalchemy 表重复定义)."""

    _name = "Cmp"

    def __init__(self, **kw):
        self.type = type("T", (), {"value": self._name})()
        for k, v in kw.items():
            setattr(self, k, v)

    @staticmethod
    def fromFileSystem(path, **_):
        return FakeCmp(file=str(path))

    @staticmethod
    def fromURL(url, **_):
        return FakeCmp(url=str(url))

    @staticmethod
    def fromBase64(b64, **_):
        return FakeCmp(file=f"base64://{b64}")

    @staticmethod
    def fromBytes(raw, **_):
        return FakeCmp(file=f"base64://{len(raw)}")


def _mk_cmp_cls(name):
    base = FakeCmp

    class _Cls(base):
        _name = name

        @staticmethod
        def fromFileSystem(path, **_):
            return _Cls(file=str(path))

        @staticmethod
        def fromURL(url, **_):
            return _Cls(url=str(url))

        @staticmethod
        def fromBase64(b64, **_):
            return _Cls(file=f"base64://{b64}")

        @staticmethod
        def fromBytes(raw, **_):
            return _Cls(file=f"base64://{len(raw)}")

    _Cls.__name__ = name
    return _Cls


@pytest.fixture(autouse=True)
def _fake_components(monkeypatch):
    monkeypatch.setattr(send, "_get_components", lambda: {
        "File": _mk_cmp_cls("File"), "Image": _mk_cmp_cls("Image"),
        "Record": _mk_cmp_cls("Record"), "Video": _mk_cmp_cls("Video"),
    })


def test_send_card_fallback_text(monkeypatch: MonkeyPatch):
    """渲染失败 → 回退纯文本 (发送仍成功)."""

    async def fake_render(result):
        raise RuntimeError("render boom")

    class FakePlain:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(send, "_plain", lambda text: FakePlain(text))
    orig = send.up_renderer
    try:
        send.up_renderer = lambda: type("R", (), {"render_image": fake_render})()
        ev = FakeEvent()
        import asyncio

        ok = asyncio.run(send.send_card(
            ev, type("R", (), {"url": "https://x"}), lambda r: "回退文本"))
        assert ok is True
        assert len(ev.sent) == 1
        assert ev.sent[0][0].text == "回退文本"
    finally:
        send.up_renderer = orig


class FailingEvent:
    def chain_result(self, segs):
        return segs

    async def send(self, segs):
        raise RuntimeError("api down")


def test_send_media_file_missing():
    """文件不存在 → 返回 False (组件 import 前的守卫, CI 可测)."""
    import asyncio
    from pathlib import Path

    ok = asyncio.run(send.send_media_file(
        FakeEvent(), Path("nonexistent/x.jpg"), "image"))
    assert ok is False


def test_no_record_frombytes_reference():
    """OneBot11 音频: 不得引用不存在的 Record.fromBytes (AstrBot 组件无此方法)."""
    src = (_ROOT / "bridge" / "send.py").read_text(encoding="utf-8")
    assert "Record.fromBytes" not in src
    assert "Record.fromBase64" in src


def test_video_file_threshold_dispatch(monkeypatch: MonkeyPatch, tmp_path):
    """OneBot11 视频分派: 超大文件 → Comp.File 文件发送 (非 base64)."""
    import asyncio

    # 生成 5MB 假视频 (超过测试阈值 1MB)
    fake_video = tmp_path / "big.mp4"
    fake_video.write_bytes(b"\x00" * (5 * 1024 * 1024))
    BridgeConfig._source = {"plite_video_file_threshold_mb": 1, "plite_use_base64": False}
    ev = FakeEvent()
    ok = asyncio.run(send.send_media_file(ev, fake_video, "video"))
    assert ok is True
    assert len(ev.sent) == 1
    assert ev.sent[0][0].type.value == "File"
    BridgeConfig._source = {}


def test_video_cover_chain(monkeypatch: MonkeyPatch, tmp_path):
    """OneBot11 视频+封面链: cover_path 存在 → 封面图前置."""
    import asyncio

    small_video = tmp_path / "small.mp4"
    small_video.write_bytes(b"\x00" * 1024)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8" + b"\x00" * 1024)
    BridgeConfig._source = {"plite_video_file_threshold_mb": 100}
    ev = FakeEvent()
    ok = asyncio.run(send.send_media_file(ev, small_video, "video", cover_path=str(cover)))
    assert ok is True
    assert len(ev.sent) == 1
    types = [s.type.value for s in ev.sent[0]]
    assert "Image" in types
    assert "Video" in types
    BridgeConfig._source = {}


def test_empty_file_intercepted(tmp_path):
    """OneBot11 空文件拦截 (0 字节 → 返回 False)."""
    import asyncio

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    ok = asyncio.run(send.send_media_file(FakeEvent(), empty, "video"))
    assert ok is False
