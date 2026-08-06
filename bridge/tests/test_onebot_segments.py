"""OneBot 11 消息段规范测试 — type/data 结构与媒体映射.

覆盖:
- _onebot11_segments: 段数组格式 (type/data, 值字符串)
- 媒体类型 → OneBot 段 type 映射 (image/record/video/file/text)
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.send as send  # noqa: E402

# OneBot 11 规范消息段 type 集合
ONEBOT11_TYPES = {
    "text",
    "face",
    "image",
    "record",
    "video",
    "at",
    "share",
    "music",
    "reply",
    "forward",
    "location",
    "node",
    "xml",
    "json",
}


class _Seg:
    def __init__(self, seg_type, **kw):
        self.type = seg_type
        for k, v in kw.items():
            setattr(self, k, v)


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
def _fake_components(monkeypatch):
    """CI 无 astrbot: 组件打桩."""
    monkeypatch.setattr(
        send,
        "_get_components",
        lambda: {
            "File": _mk_cmp_cls("File"),
            "Image": _mk_cmp_cls("Image"),
            "Record": _mk_cmp_cls("Record"),
            "Video": _mk_cmp_cls("Video"),
        },
    )


def test_segments_structure():
    """段数组: [{"type": ..., "data": {...}}] 值均为字符串."""
    segs = send._onebot11_segments(
        [
            _Seg("text", text="hello"),
            _Seg("image", file="base64://abc"),
            _Seg("video", file="file://x"),
        ]
    )
    assert segs == [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "image", "data": {"file": "base64://abc"}},
        {"type": "video", "data": {"file": "file://x"}},
    ]


def test_segment_types_in_spec():
    """发送层产生的段 type ∈ OneBot 11 规范集合 (含组件枚举值)."""
    # AstrBot 组件 type 枚举值 (ComponentType)
    segs = send._onebot11_segments(
        [
            _Seg("Image", file="x"),
            _Seg("Record", file="x"),
            _Seg("Video", file="x"),
        ]
    )
    types = {s["type"] for s in segs}
    assert types, "应产生段"
    # 组件枚举 (Image/Record/Video) 或规范名 (image/record/video)
    for t in types:
        assert t in ONEBOT11_TYPES or t.lower() in ONEBOT11_TYPES
