"""双层防抖 (E5) — 链接级持久化防抖 + 失败回滚.

- 链接防抖: {session, url} → 时间窗, JSON 持久化 (跨重启)
- 失败回滚: 解析失败时从防抖池移除, 允许重试
- 配置驱动: 窗口秒数动态传入, 0 硬编码
"""

from __future__ import annotations

import json
from pathlib import Path
import time


class Debouncer:
    def __init__(self, state_path: str | Path | None = None):
        self._state_path = Path(state_path) if state_path else None
        self._hits: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            self._hits = json.loads(self._state_path.read_text("utf-8"))
        except Exception:
            self._hits = {}

    def save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._hits), encoding="utf-8")
        except Exception:
            pass

    def should_parse(self, key: str, window_sec: float) -> bool:
        """时间窗内已解析过 → 返回 False (防抖命中)."""
        now = time.time()
        last = self._hits.get(key, 0.0)
        if last and now - last < window_sec:
            return False
        self._hits[key] = now
        self.save()
        return True

    def mark_success(self, key: str) -> None:
        """解析成功 → 记录时间戳 (防抖窗口起点)."""
        self._hits[key] = time.time()
        self.save()

    def rollback(self, key: str) -> None:
        """解析失败 → 移除防抖记录, 允许重试 (devil233 rollback_url 模式)."""
        self._hits.pop(key, None)
        self.save()

    def clear(self) -> None:
        self._hits.clear()
        self.save()


def make_debouncer(base_dir: str | Path) -> Debouncer:
    return Debouncer(Path(base_dir) / "debounce.json")


def debounce_key(session: str, url: str) -> str:
    return f"{session}:{url}"
