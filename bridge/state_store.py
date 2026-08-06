"""统一 JSON 状态存储 — 锁保护 + 写节流 + 原子落盘.

合并 debounce/rate_limit/push/cookie_health 的重复 JSON 持久化:
- threading.Lock 保护内存变更 (同步/异步调用均安全, 不阻塞事件循环)
- 写节流 (write-coalescing): 累计 N 次变更或距上次落盘超时 → 原子写
  (tmp 文件 + os.replace), 崩溃一致性
- 内存数据与落盘共享同一 dict 引用 (调用方直接读)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


class JsonStateStore:
    def __init__(self, path: str | Path | None = None,
                 flush_every: int = 10, flush_interval: float = 5.0):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {}
        self._mutations = 0
        self._last_flush = time.time()  # 构造时起算, 避免首次 update 误判超时
        self._flush_every = max(flush_every, 1)
        self._flush_interval = max(flush_interval, 0.5)
        self._load()

    @property
    def data(self) -> dict:
        """共享数据视图 (调用方直接读; 变更须经 update)."""
        return self._data

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception:
            self._data = {}

    def _flush_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _tmp = self._path.with_name(self._path.name + ".tmp")
            _tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(_tmp, self._path)  # 原子替换, 崩溃不损坏
            self._last_flush = time.time()
            self._mutations = 0
        except Exception:
            pass

    def update(self, fn) -> None:
        """锁内执行变更 fn(data) + 写节流落盘."""
        if self._path is None:
            fn(self._data)
            return
        with self._lock:
            fn(self._data)
            self._mutations += 1
            now = time.time()
            if (self._mutations >= self._flush_every
                    or now - self._last_flush >= self._flush_interval):
                self._flush_locked()

    def flush(self) -> None:
        """显式落盘 (进程退出/命令触发)."""
        if self._path is None:
            return
        with self._lock:
            self._flush_locked()

    def reset(self, data: dict | None = None) -> None:
        """清空/替换 (测试用)."""
        with self._lock:
            self._data = dict(data) if data is not None else {}
            self._mutations = 0
