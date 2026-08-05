"""媒体 md5 缓存 — QQ 服务器端资源引用秒回应 (参考 SnowLuma fast-upload 语义).

原理 (OneBot11 / NapCat / Lagrange / SnowLuma):
- QQ 服务器按资源指纹 (md5/sha1) 缓存媒体; 相同内容再次发送时,
  客户端可用 file://<md5> 引用直接发送 (秒回应, 不重新上传)
- SnowLuma highway 的 fast-upload: 有指纹不传 bytes; 服务器要求数据时
  (fastOnlyError) 回退全量上传 — 本模块同语义: file://md5 尝试失败 → 回退正常路径

设计:
- 持久化缓存 state_dir()/media_md5.json: {md5: {type, size, ts}}
- md5 全量流式计算 (不整读内存, 大文件可用 max_bytes 截断策略)
- 上限条目 (plite_md5_cache_max), LRU 淘汰
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import time

_logger = logging.getLogger("parser-lite.bridge.media_cache")


def compute_md5(path: str | Path, chunk: int = 1024 * 1024) -> str:
    """流式计算文件 md5 (hex)."""
    _h = hashlib.md5()
    with open(path, "rb") as _f:
        while True:
            _b = _f.read(chunk)
            if not _b:
                break
            _h.update(_b)
    return _h.hexdigest()


def md5_file_ref(md5_hex: str) -> str:
    """OneBot11 资源引用: file://<md5> (客户端识别 → QQ 服务器资源秒发)."""
    return f"file://{md5_hex.strip().lower()}"


def is_md5_ref(file_value: str) -> bool:
    """判断 file 字段是否为 md5 引用 (file:// + 32hex)."""
    if not isinstance(file_value, str) or not file_value.startswith("file://"):
        return False
    _rest = file_value[len("file://"):].split(".")[0]
    return len(_rest) == 32 and all(c in "0123456789abcdef" for c in _rest)


class MediaMd5Cache:
    """md5 → {type, size, ts} 持久化缓存 (LRU 上限)."""

    def __init__(self, cache_path: str | Path | None = None, max_entries: int = 200):
        self._path = Path(cache_path) if cache_path else None
        self._max = max(max_entries, 1)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items()
                              if isinstance(k, str) and isinstance(v, dict)}
        except Exception:
            _logger.warning("[ParserLite] media_md5 cache 读取失败, 重建")

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except Exception:
            pass

    def lookup(self, md5_hex: str) -> dict | None:
        return self._data.get(md5_hex.lower())

    def has(self, md5_hex: str) -> bool:
        return md5_hex.lower() in self._data

    def put(self, md5_hex: str, media_type: str, size: int) -> None:
        _k = md5_hex.lower()
        self._data[_k] = {"type": media_type, "size": int(size), "ts": time.time()}
        if len(self._data) > self._max:
            for _old in sorted(self._data, key=lambda k: self._data[k].get("ts", 0))[:len(self._data) - self._max]:
                self._data.pop(_old, None)
        self.save()

    def purge(self) -> None:
        self._data.clear()
        self.save()

    def __len__(self) -> int:
        return len(self._data)


_CACHE: MediaMd5Cache | None = None


def get_cache(max_entries: int = 200) -> MediaMd5Cache:
    """全局缓存 (延迟初始化, 路径统一)."""
    global _CACHE
    if _CACHE is None:
        from bridge.paths import state_dir

        _CACHE = MediaMd5Cache(state_dir() / "media_md5.json", max_entries=max_entries)
    return _CACHE


def reset_cache() -> None:
    """测试用: 重置全局缓存实例."""
    global _CACHE
    _CACHE = None
