"""md5 指纹缓存 + file://md5 秒传测试 (参考 SnowLuma fast-upload 语义)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from _pytest.monkeypatch import MonkeyPatch
import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.context import BridgeConfig  # noqa: E402
from bridge.media_cache import (  # noqa: E402
    MediaMd5Cache,
    compute_md5,
    is_md5_ref,
    md5_file_ref,
    reset_cache,
)
import bridge.send as send  # noqa: E402


def test_compute_md5_stable(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"hello world" * 100)
    assert compute_md5(f) == compute_md5(f)
    assert len(compute_md5(f)) == 32


def test_md5_file_ref_format():
    ref = md5_file_ref("A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6")
    assert ref == "file://a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    assert is_md5_ref(ref) is True
    assert is_md5_ref("file://not-md5") is False
    assert is_md5_ref("C:/local/path.jpg") is False


def test_cache_persist(tmp_path):
    c = MediaMd5Cache(tmp_path / "md5.json", max_entries=5)
    c.put("a" * 32, "image", 100)
    c.put("b" * 32, "video", 200)
    c.save()  # 显式落盘 (节流下不自动)
    c2 = MediaMd5Cache(tmp_path / "md5.json", max_entries=5)
    assert c2.has("a" * 32)
    assert c2.lookup("b" * 32)["type"] == "video"


def test_cache_lru_evict(tmp_path):
    c = MediaMd5Cache(tmp_path / "md5.json", max_entries=2)
    c.put("a" * 32, "image", 1)
    c.put("b" * 32, "image", 1)
    c.put("c" * 32, "image", 1)  # 淘汰 a
    assert not c.has("a" * 32)
    assert c.has("b" * 32)
    assert c.has("c" * 32)


class FakeEvent:
    def __init__(self):
        self.sent = []

    def chain_result(self, segs):
        return segs

    async def send(self, segs):
        self.sent.append(segs)


class FakeCmp:
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
    class _Cls(FakeCmp):
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
def _fake_env(monkeypatch: MonkeyPatch, tmp_path):
    reset_cache()
    monkeypatch.setattr(send, "_get_components", lambda: {
        "File": _mk_cmp_cls("File"), "Image": _mk_cmp_cls("Image"),
        "Record": _mk_cmp_cls("Record"), "Video": _mk_cmp_cls("Video"),
    })
    from bridge.media_cache import _CACHE as _c
    if _c is not None:
        _c._path = tmp_path / "md5.json"
    yield
    BridgeConfig._source = {}
    reset_cache()


def test_md5_second_send_uses_file_ref(tmp_path):
    """首次发送记录 md5; 二次发送相同内容 → file://md5 引用秒发."""

    BridgeConfig._source = {"plite_md5_fast_send": True, "plite_use_base64": False}
    media = tmp_path / "img.jpg"
    media.write_bytes(b"\xff\xd8" + b"\x00" * 2048)
    ev = FakeEvent()
    ok1 = asyncio.run(send.send_media_file(ev, media, "image"))
    assert ok1
    assert ev.sent[0][0].type.value == "Image"  # 首次正常上传

    ev2 = FakeEvent()
    ok2 = asyncio.run(send.send_media_file(ev2, media, "image"))
    assert ok2
    assert ev2.sent[0][0].type.value == "Image"
    # 二次走 md5 引用 (file://md5)
    assert is_md5_ref(ev2.sent[0][0].file) is True


def test_md5_ref_failure_falls_back(tmp_path):
    """md5 引用发送失败 → 回退正常路径 (多级 failback)."""

    BridgeConfig._source = {"plite_md5_fast_send": True, "plite_use_base64": False}
    media = tmp_path / "img.jpg"
    media.write_bytes(b"\xff\xd8" + b"\x00" * 2048)

    class RefFailingEvent:
        def __init__(self):
            self.sent = []
            self._first = True

        def chain_result(self, segs):
            return segs

        async def send(self, segs):
            # 第一次 (md5 引用) 失败, 后续正常
            if self._first and any(is_md5_ref(getattr(s, "file", "")) for s in segs):
                self._first = False
                raise RuntimeError("md5 ref not supported")
            self.sent.append(segs)

    ev = RefFailingEvent()
    ok1 = asyncio.run(send.send_media_file(ev, media, "image"))
    assert ok1
    # 二次调用同样内容: md5 引用又失败 → 回退成功
    ev2 = RefFailingEvent()
    ok2 = asyncio.run(send.send_media_file(ev2, media, "image"))
    assert ok2
    assert ev2.sent, "应回退到正常路径发送成功"


def test_md5_fast_send_disabled(tmp_path):
    """plite_md5_fast_send=False → 不走 md5 引用."""

    BridgeConfig._source = {"plite_md5_fast_send": False, "plite_use_base64": False}
    media = tmp_path / "img.jpg"
    media.write_bytes(b"\xff\xd8" + b"\x00" * 2048)
    ev = FakeEvent()
    ok = asyncio.run(send.send_media_file(ev, media, "image"))
    assert ok
    assert is_md5_ref(getattr(ev.sent[0][0], "file", "")) is False
