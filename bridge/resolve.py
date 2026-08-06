"""解析适配 — 委托上游 pipeline.Parser (r8: 完全解耦自研双轨).

上游原生调用链 (与 nonebot-plugin-parser-lite 一致):
  Parser.match(text) → _key_patterns 关键词正则 (同 bridge search_url 数据源)
  Parser.parse(text, until=PARSE) → match → 上游结果缓存 → parser.parse
bridge 仅做 AstrBot 平台适配: 配置热载 / cookies 同步 / 直连客户端重建 / 超时守卫.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bridge.context import BridgeConfig
from bridge.proxy import apply_downloader_proxy, sync_cookies_to_upstream

PARSE_TIMEOUT = 60.0  # 单次解析总超时(秒) — 防慢解析拖死

_logger = logging.getLogger("nonebot_plugin_parser_lite")


def _sync_enabled_to_upstream() -> None:
    """bridge platforms.items.enabled 勾选 → 上游 pconfig.disabled_platforms.

    上游 Parser._types() 用 pconfig.disabled_platforms 过滤; 委托后由
    bridge 勾选驱动 (未配置勾选 = 全部启用, 与 bridge 语义一致).
    """
    try:
        from bridge.proxy import enabled_platforms
        from nonebot_plugin_parser_lite.config import pconfig
        from nonebot_plugin_parser_lite.constants import PlatformEnum

        _en = enabled_platforms()
        if _en is None:
            pconfig.disabled_platforms = []
            return
        _all = {p.name for p in PlatformEnum}
        _disabled = [p for p in _all if p not in _en]
        if list(pconfig.disabled_platforms) != _disabled:
            pconfig.disabled_platforms = _disabled
    except Exception:
        pass


class ParserLite:
    """薄解析编排器: 委托上游 pipeline.Parser, 仅做 AstrBot 适配."""

    def __init__(self, **config_kwargs):
        if config_kwargs:
            BridgeConfig.configure(**config_kwargs)

    async def parse_url(self, url: str) -> Any:
        """解析 URL → 上游 ParseResult (薄包装: 配置/同步/重建/超时).

        上游 Parser 自带: 平台匹配 (_key_patterns), 结果缓存,
        disabled_platforms 过滤, 解析器实例管理.
        """
        BridgeConfig.configure()
        # 平台 cookie 同步: platforms.cookies 条目 → 上游 plite_*_ck (动态源, 幂等)
        try:
            sync_cookies_to_upstream()
        except Exception:
            pass
        # T2: 直连客户端重建 (插件重载后 DOWNLOADER 残留清理)
        apply_downloader_proxy("")
        # bridge enabled 勾选 → 上游 disabled 过滤同步
        _sync_enabled_to_upstream()
        from nonebot_plugin_parser_lite.pipeline import Parser as UpParser
        from nonebot_plugin_parser_lite.pipeline import ParseStep

        try:
            async with UpParser() as parser:
                return await asyncio.wait_for(
                    parser.parse(url, until=ParseStep.PARSE), timeout=PARSE_TIMEOUT
                )
        except asyncio.TimeoutError as _e:
            raise TimeoutError(f"解析超时 ({PARSE_TIMEOUT:.0f}s): {url[:60]}") from _e

    async def close(self):
        """关闭上游运行时 (DOWNLOADER + scheduler + BrowserManager)."""
        try:
            from nonebot_plugin_parser_lite.pipeline import shutdown_runtime

            await shutdown_runtime()
        except Exception:
            pass
