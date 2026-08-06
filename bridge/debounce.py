"""双层防抖 (E5) — 链接级持久化防抖 + 失败回滚.

- 链接防抖: {session, url} → 时间窗, JSON 持久化 (跨重启)
- 失败回滚: 解析失败时从防抖池移除, 允许重试
- 配置驱动: 窗口秒数动态传入, 0 硬编码
- 并发安全: JsonStateStore (锁 + 写节流 + 原子落盘)
"""

from __future__ import annotations

from pathlib import Path
import time

from bridge.state_store import JsonStateStore


class Debouncer:
    def __init__(self, state_path: str | Path | None = None):
        # 即时落盘 (原子写): 防抖记录跨重启即时性优先; 节流仅用于高频状态 (push/cookie_health)
        self._store = JsonStateStore(state_path, flush_every=1, flush_interval=0.5)
        self._hits = self._store.data  # 共享 dict 引用

    def save(self) -> None:
        """显式落盘 (兼容旧调用)."""
        self._store.flush()

    def should_parse(self, key: str, window_sec: float) -> bool:
        """时间窗内已解析过 → 返回 False (防抖命中)."""
        now = time.time()
        last = self._hits.get(key, 0.0)
        if last and now - last < window_sec:
            return False

        def _set(d):
            d[key] = now

        self._store.update(_set)
        return True

    def mark_success(self, key: str) -> None:
        """解析成功 → 记录时间戳 (防抖窗口起点)."""

        def _set(d):
            d[key] = time.time()

        self._store.update(_set)

    def rollback(self, key: str) -> None:
        """解析失败 → 移除防抖记录, 允许重试 (devil233 rollback_url 模式)."""

        def _rm(d):
            d.pop(key, None)

        self._store.update(_rm)

    def clear(self) -> None:
        self._store.reset()


def make_debouncer(base_dir: str | Path) -> Debouncer:
    return Debouncer(Path(base_dir) / "debounce.json")


def debounce_key(session: str, url: str) -> str:
    return f"{session}:{url}"


def load_cfg(source: dict | None = None) -> dict:
    """提取 debounce 配置段 (ttl 秒, 可注入配置源)."""
    from bridge.cfg import global_source, read_cfg

    src = source if source is not None else global_source()
    ttl = read_cfg(src, "plite_dedup_ttl", 60)
    return {"ttl_sec": float(60 if ttl is None else ttl)}
