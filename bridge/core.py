"""Bridge core (薄 re-export 层) — 功能已迁入 context/proxy/resolve/inject.

架构 (参考 PR #1 薄桥接):
- context.py   : 上游引用聚合 + BridgeConfig 单例
- proxy.py     : 代理注入 + 平台决策 + 特征表
- resolve.py   : ParserLite 薄解析编排
- inject.py    : 0 硬编码动态注入决策树
- core.py      : 兼容 re-export + 独有功能 (CustomParser/LazyManager/disabled_groups)
"""

# ruff: noqa: F401
from __future__ import annotations

import asyncio  # noqa: F401  (LazyManager 使用)
import os
from pathlib import Path
import time

from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict


def _get_logger():
    """惰性获取日志器 — 无 astrbot 环境 (CI/离线测试) 时回退标准 logging."""
    try:
        from astrbot.api import logger as _l
        return _l
    except Exception:
        import logging
        return logging.getLogger("parser-lite.bridge.core")


astrbot_logger = _get_logger()

# ── re-export: 配置/上下文 ──────────────────────────────────────────────────
from bridge.context import (  # noqa: F401,E402
    BridgeConfig,
    configure,
    get_config,
)
from bridge.context import (
    label as _label,
)

# ── re-export: 代理/平台决策/特征表 ─────────────────────────────────────────
from bridge.proxy import (  # noqa: F401,E402
    PROXY_PROTOCOLS as _PROXY_PROTOCOLS,
)
from bridge.proxy import (
    apply_downloader_proxy as _apply_downloader_proxy,
)
from bridge.proxy import (
    build_feature_table,
    target_uses_proxy,
)
from bridge.proxy import (
    client_closed as _client_closed,
)
from bridge.proxy import (
    get_cookies_for as _get_cookies_for,
)
from bridge.proxy import (
    load_parsers_config as _load_parsers_config,
)
from bridge.proxy import (
    platform_cfg as _platform_cfg,
)
from bridge.proxy import (
    read_proxy_config as _read_proxy_config,
)
from bridge.proxy import (
    resolve_proxy_url as _resolve_proxy_url,
)
from bridge.proxy import (
    use_proxy_for as _use_proxy_for,
)

# ── re-export: 解析编排 ─────────────────────────────────────────────────────
from bridge.resolve import PARSE_TIMEOUT, ParserLite  # noqa: F401,E402

# ── 常量/状态 (保留) ────────────────────────────────────────────────────────
CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict = LimitedSizeDict(max_size=50)
FEATURE_TABLE: dict[str, str] = {}


def _build_feature_table():
    """重建动态特征表 (0 hardcode)."""
    FEATURE_TABLE.clear()
    FEATURE_TABLE.update(build_feature_table())


def _is_parser_enabled(platform: str) -> bool:
    """平台启用: platforms.items.enabled 勾选 (默认全部), 兼容旧 enable/disabled_platforms."""
    try:
        from bridge.proxy import enabled_platforms as _enabled_platforms

        _en = _enabled_platforms()
        if _en is not None:
            return platform.lower() in _en
        _pc = _platform_cfg(platform)
        if "enable" in _pc:
            return bool(_pc["enable"])
        from bridge.resolve import BridgeConfig
        cfg = BridgeConfig.get_config()
        return platform not in [
            p.name.lower() if hasattr(p, "name") else str(p).lower()
            for p in (cfg.disabled_platforms if hasattr(cfg, "disabled_platforms") else [])
        ]
    except Exception:
        return True


_DISABLED_GROUPS_FILE = None  # 延迟解析 (统一路径, 消除 __file__ 环境差异)


def _disabled_groups_path() -> Path:
    global _DISABLED_GROUPS_FILE
    if _DISABLED_GROUPS_FILE is None:
        from bridge.paths import ensure_state_dir

        _DISABLED_GROUPS_FILE = ensure_state_dir() / "disabled_groups.json"
    return _DISABLED_GROUPS_FILE


def _load_disabled_groups() -> set[str]:
    try:
        _f = _disabled_groups_path()
        if _f.exists():
            import json
            return set(json.loads(_f.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save_disabled_groups(data: set[str]) -> None:
    try:
        import json
        _f = _disabled_groups_path()
        _f.parent.mkdir(parents=True, exist_ok=True)
        _f.write_text(json.dumps(sorted(data)), encoding="utf-8")
    except Exception:
        pass


def _detect_missing_libs() -> str:
    """检测 Chromium 缺失系统库 (Linux)."""
    import ctypes
    import ctypes.util

    libs = {"libnspr4.so": "nspr4", "libnss3.so": "nss3", "libgbm.so.1": "gbm",
            "libasound.so.2": "asound", "libxkbcommon.so.0": "xkbcommon"}
    missing = [s for s, n in libs.items()
               if not (ctypes.util.find_library(n) and _try_load(ctypes.util.find_library(n)))]
    return "\n".join(missing)


def _try_load(path):
    import ctypes
    try:
        ctypes.cdll.LoadLibrary(path)
        return True
    except OSError:
        return False


# ── CustomParser (保留, 上游无关的自定义解析器) ─────────────────────────────
from bridge.custom_parser import CustomParser  # noqa: F401,E402


# ── LazyManager (保留: 懒下载会话) ──────────────────────────────────────────
class LazyManager:
    Session = None  # type: ignore[assignment]
    _lock = None  # 延迟初始化 (threading.Lock)

    @classmethod
    def _get_lock(cls):
        if cls._lock is None:
            import threading
            cls._lock = threading.Lock()
        return cls._lock

    @classmethod
    def add(cls, key: str, result, url: str, timeout_sec: float) -> None:
        import asyncio
        if cls.Session is None:
            from dataclasses import dataclass

            @dataclass
            class _Session:
                result: object
                url: str
                task: object
                deadline: float
            cls.Session = _Session
        cls.remove(key)
        task = asyncio.create_task(cls._timeout_handler(key, timeout_sec))
        with cls._get_lock():
            cls._sessions[key] = cls.Session(result=result, url=url, task=task,
                                             deadline=time.time() + timeout_sec)

    @classmethod
    def get(cls, key: str):
        with cls._get_lock():
            return cls._sessions.get(key)

    @classmethod
    def remove(cls, key: str) -> None:
        with cls._get_lock():
            s = cls._sessions.pop(key, None)
        if s and getattr(s, "task", None):
            s.task.cancel()

    @classmethod
    async def _timeout_handler(cls, key: str, timeout_sec: float) -> None:
        await asyncio.sleep(timeout_sec)
        cls.remove(key)

    @classmethod
    def cleanup(cls) -> int:
        with cls._get_lock():
            n = len(cls._sessions)
            cls._sessions.clear()
        return n


LazyManager._sessions: dict = {}
