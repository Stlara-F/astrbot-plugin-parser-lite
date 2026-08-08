"""Bridge core (薄 re-export 层) — 功能已迁入 context/proxy/resolve/inject.

架构 (参考 PR #1 薄桥接):
- context.py   : 上游引用聚合 + BridgeConfig 单例
- proxy.py     : 代理注入 + 平台决策
- resolve.py   : ParserLite 薄解析编排
- inject.py    : 0 硬编码动态注入决策树
- core.py      : 兼容 re-export + disabled_groups 持久化 + 环境检测
"""

# ruff: noqa: F401
from __future__ import annotations

import os
from pathlib import Path
import time


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
# ── re-export: 代理/平台决策 ────────────────────────────────────────────────
from bridge.adapter import (  # noqa: F401,E402
    apply_downloader_proxy as _apply_downloader_proxy,
)
from bridge.adapter import (
    client_closed as _client_closed,
)
from bridge.adapter import (
    get_cookies_for as _get_cookies_for,
)
from bridge.adapter import (
    load_parsers_config as _load_parsers_config,
)
from bridge.adapter import (
    platform_cfg as _platform_cfg,
)
from bridge.config import (  # noqa: F401,E402
    BridgeConfig,
    configure,
    get_config,
)
from bridge.config import (  # noqa: F401,E402
    label as _label,
)

# ── re-export: 解析编排 ─────────────────────────────────────────────────────
from bridge.pipeline import PARSE_TIMEOUT, ParserLite  # noqa: F401,E402


def _is_parser_enabled(platform: str) -> bool:
    """平台启用判定 (r9: 与 enabled 列表收敛, 无配置 → 全部启用).

    优先级: platforms.items.enabled 勾选 → 旧模板 enable → True.
    """
    try:
        from bridge.adapter import enabled_platforms

        _en = enabled_platforms()
        if _en is not None:
            return platform.lower() in _en
        _pc = _platform_cfg(platform)
        if "enable" in _pc:
            return bool(_pc["enable"])
        # 显式语义: 未配置勾选 → 全部启用 (设计默认)
        return True
    except Exception:
        return True


_DISABLED_GROUPS_FILE = None  # 延迟解析 (统一路径, 消除 __file__ 环境差异)
_DISABLED_GROUPS_STORE = None  # JsonStateStore (B6: 统一锁/原子写/节流)


def _disabled_groups_path() -> Path:
    global _DISABLED_GROUPS_FILE
    if _DISABLED_GROUPS_FILE is None:
        from bridge.config import ensure_state_dir

        _DISABLED_GROUPS_FILE = ensure_state_dir() / "disabled_groups.json"
    return _DISABLED_GROUPS_FILE


def _disabled_groups_store():
    """禁用群组状态存储 (JsonStateStore: 锁 + 原子写 + 节流)."""
    global _DISABLED_GROUPS_STORE
    if _DISABLED_GROUPS_STORE is None:
        from bridge.config import JsonStateStore

        _DISABLED_GROUPS_STORE = JsonStateStore(
            _disabled_groups_path(), flush_every=1, flush_interval=0.5
        )
    return _DISABLED_GROUPS_STORE


def _load_disabled_groups() -> set[str]:
    try:
        _store = _disabled_groups_store()
        _raw = _store.data
        if isinstance(_raw, dict):
            return {str(k) for k in _raw}
        # 旧格式 (JSON list) 迁移 → dict 结构
        _f = _disabled_groups_path()
        if _f.exists():
            import json

            _legacy = json.loads(_f.read_text(encoding="utf-8"))
            if isinstance(_legacy, list):
                _store.update(lambda d: d.update({str(g): 1 for g in _legacy}))
                _store.flush()
                return {str(g) for g in _legacy}
    except Exception:
        pass
    return set()


def _save_disabled_groups(data: set[str]) -> None:
    try:
        _store = _disabled_groups_store()
        _store.update(lambda d: (d.clear(), d.update({str(g): 1 for g in data}))[1])
        _store.flush()  # 命令触发场景即时落盘
    except Exception:
        pass


def _detect_missing_libs() -> str:
    """检测 Chromium 缺失系统库 (B19: 仅 Linux, Windows/macOS 不误报)."""
    import sys as _sys

    if _sys.platform != "linux":
        return ""
    import ctypes
    import ctypes.util

    libs = {
        "libnspr4.so": "nspr4",
        "libnss3.so": "nss3",
        "libgbm.so.1": "gbm",
        "libasound.so.2": "asound",
        "libxkbcommon.so.0": "xkbcommon",
    }
    missing = [
        s
        for s, n in libs.items()
        if not (ctypes.util.find_library(n) and _try_load(ctypes.util.find_library(n)))
    ]
    return "\n".join(missing)


def _try_load(path):
    import ctypes

    try:
        ctypes.cdll.LoadLibrary(path)
        return True
    except OSError:
        return False
