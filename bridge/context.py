"""桥接上下文: 上游引用聚合 (R3: BridgeConfig 已迁 bridge/config).

薄桥接核心: 所有对上游 (nonebot_plugin_parser_lite) 的引用在此聚合,
bridge 其余模块只经此访问上游, 上游不反向依赖 bridge.
"""

from __future__ import annotations

from typing import Any

# ── 上游引用 (延迟 import: CI/离线测试无上游时保持可导入) ──────────────────

_UP_CONFIG: Any = None
_UP_DOWNLOADER: Any = None
_UP_RENDERER: Any = None
_UP_BASE_PARSER: Any = None
_UP_CREATOR: Any = None


def _import_upstream() -> None:
    """按需加载上游模块 (standalone 模式)."""
    global _UP_CONFIG, _UP_DOWNLOADER, _UP_RENDERER, _UP_BASE_PARSER, _UP_CREATOR
    if _UP_CONFIG is None:
        from nonebot_plugin_parser_lite.config import Config as _UP_CONFIG
    if _UP_DOWNLOADER is None:
        from nonebot_plugin_parser_lite.download import DOWNLOADER as _UP_DOWNLOADER
    if _UP_RENDERER is None:
        from nonebot_plugin_parser_lite.render import RENDERER as _UP_RENDERER
    if _UP_BASE_PARSER is None:
        from nonebot_plugin_parser_lite.parsers.base import (
            BaseParser as _UP_BASE_PARSER,
        )
    if _UP_CREATOR is None:
        from nonebot_plugin_parser_lite.creator import Creator as _UP_CREATOR


def up_config():
    _import_upstream()
    return _UP_CONFIG


def up_downloader():
    _import_upstream()
    return _UP_DOWNLOADER


def up_renderer():
    _import_upstream()
    # 自动确保渲染补丁 (safe_src 默认 method + pl_esc/pl_str 注册, 幂等)
    # 上游模板省略 method 且引用 pl_esc/pl_str — 任何渲染调用方都需要
    try:
        from bridge.render import apply_render_patch

        apply_render_patch()
        # 引用对齐: main.py 清 sys.modules 会重建上游模块 → 缓存必须跟随
        # (否则 patch 打到新模块实例, 调用仍走旧实例 → pl_esc 未注册)
        import nonebot_plugin_parser_lite.render as _render_mod

        global _UP_RENDERER
        _UP_RENDERER = _render_mod.RENDERER
    except Exception:
        pass
    return _UP_RENDERER


def up_base_parser():
    _import_upstream()
    # 惰性发现: 显式注册全部平台解析器
    from nonebot_plugin_parser_lite.parsers import load_all as _load_all

    _load_all()
    return _UP_BASE_PARSER


def up_creator():
    _import_upstream()
    return _UP_CREATOR
