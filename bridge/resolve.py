"""解析适配: 调用上游解析器原始方法, 薄编排.

原始调用链保留 (与上游一致):
  cls.search_url(url) → parser.parse(kw, mwp) → 上游解析器内部逻辑
bridge 仅在其前后做适配: 配置热载 / 代理语义 / 超时守卫 / httpx 重建.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bridge.cfg import global_source
from bridge.context import BridgeConfig, up_base_parser, up_downloader
from bridge.proxy import (
    PROXY_PROTOCOLS,
    apply_downloader_proxy,
    build_feature_table,
    read_proxy_config,
    target_uses_proxy,
)

PARSE_TIMEOUT = 60.0  # 单次解析总超时(秒) — 防慢代理/死链拖死


class ParserLite:
    """薄解析编排器: 仅路由 + 调用上游解析器, 不做数据干预."""

    def __init__(self, **config_kwargs):
        if config_kwargs:
            BridgeConfig.configure(**config_kwargs)
        self._parsers: dict[str, Any] = {}
        self._custom_parsers: list[Any] | None = None
        self._feature_table: dict[str, str] | None = None

    def _route_url(self, url: str) -> str | None:
        if self._feature_table is None:
            self._feature_table = build_feature_table()
        for pattern, name in self._feature_table.items():
            if pattern in url:
                return name
        return None

    async def parse_url(self, url: str) -> Any:
        """解析 URL → 上游 ParseResult (薄封装: 超时/代理语义/httpx 守卫)."""
        BridgeConfig.configure()
        # 平台 cookie 同步: platforms.cookies 条目 → 上游 plite_*_ck (动态源, 幂等)
        try:
            from bridge.proxy import sync_cookies_to_upstream

            sync_cookies_to_upstream()
        except Exception:
            pass
        proxy_url = read_proxy_config()
        target = self._route_url(url)
        ordered = list(up_base_parser().get_all_subclass())
        if target:
            ordered = [c for c in ordered if c.__name__ == target] + \
                      [c for c in ordered if c.__name__ != target]
        self._ensure_parser_httpx(ordered)
        # 默认直连; 代理为附加配置: 仅 platforms[].proxy 勾选的平台走代理
        if proxy_url and target_uses_proxy(ordered, target):
            return await self._parse_via_proxy(ordered, url, proxy_url)
        # 直连 (默认)
        apply_downloader_proxy("")
        try:
            return await asyncio.wait_for(
                self._try_all_parsers(ordered, url), timeout=PARSE_TIMEOUT)
        except asyncio.TimeoutError as _e:
            raise TimeoutError(f"解析超时 ({PARSE_TIMEOUT:.0f}s): {url[:60]}") from _e
        except Exception:
            return await self._try_custom_parsers(url)

    async def _parse_via_proxy(self, ordered: list, url: str, proxy_url: str):
        """代理轮询 (协议逐试) + 全失败回退直连."""
        import logging

        _logger = logging.getLogger("nonebot_plugin_parser_lite")
        _try_protocols = PROXY_PROTOCOLS if "://" not in proxy_url else (proxy_url,)
        _last_err = None
        for _proto in _try_protocols:
            _px = _proto + proxy_url if "://" not in proxy_url else proxy_url
            apply_downloader_proxy(_px)
            try:
                return await asyncio.wait_for(
                    self._try_all_parsers(ordered, url), timeout=PARSE_TIMEOUT)
            except asyncio.TimeoutError:
                _last_err = TimeoutError(f"解析超时 ({PARSE_TIMEOUT:.0f}s): {url[:60]}")
                _logger.warning(f"[ParserLite] proxy {_px} 超时, 切换协议")
                continue
            except Exception as _e:
                _last_err = _e
                _logger.debug(f"[ParserLite] proxy {_px} failed: {_e}")
                continue
        _logger.warning(f"[ParserLite] 代理全部失败, 回退直连: {url[:60]}")
        apply_downloader_proxy("")
        try:
            return await asyncio.wait_for(
                self._try_all_parsers(ordered, url), timeout=PARSE_TIMEOUT)
        except asyncio.TimeoutError as _e:
            raise TimeoutError(f"解析超时 ({PARSE_TIMEOUT:.0f}s): {url[:60]}") from _e
        except Exception as _e2:
            raise _last_err if _last_err is not None else _e2  # type: ignore[misc]

    async def _try_all_parsers(self, ordered: list, url: str) -> Any:
        """遍历解析器, 调用上游原始 search_url/parse (保留原始调用)."""
        import logging

        _logger = logging.getLogger("nonebot_plugin_parser_lite")
        from bridge.proxy import get_cookies_for

        _matched_err = None
        for parser_cls in ordered:
            try:
                kw, mwp = parser_cls.search_url(url)
            except Exception:
                continue
            if not kw:
                continue
            try:
                parser = self._get_parser(parser_cls)
                cookies = get_cookies_for(
                    str(getattr(getattr(parser_cls, "platform", None), "name", "")).lower())
                if cookies and str(getattr(getattr(parser_cls, "platform", None), "name", "")).lower() == "bilibili":
                    # B4: 统一入口写入 (source 原位更新 + 显式 configure 刷新)
                    from bridge.cfg import set_plite_bili_ck

                    set_plite_bili_ck(next(iter(cookies.values())) or "")
                return await parser.parse(kw, mwp)
            except Exception as e:
                _matched_err = e
                _logger.warning(
                    f"[ParserLite] {parser_cls.__name__} matched but failed: {e}")
        if _matched_err is not None:
            raise _matched_err
        raise ValueError(f"Unsupported URL: {url}")

    async def _try_custom_parsers(self, url: str) -> Any:
        self._load_custom_parsers()
        for cp in self._custom_parsers:
            try:
                kw, mwp = cp.search_url(url)
                if not kw:
                    continue
                return await cp.parse(kw, mwp)
            except Exception as e:
                import logging
                logging.getLogger("nonebot_plugin_parser_lite").warning(
                    f"[ParserLite] CustomParser failed: {e}")
        raise ValueError(f"Unsupported URL: {url}")

    def _load_custom_parsers(self):
        if self._custom_parsers is not None:
            return
        import json as _json

        from bridge.core import CustomParser

        self._custom_parsers = []
        source = global_source()
        entries = source.get("custom_parsers", [])
        if isinstance(entries, str):
            try:
                entries = _json.loads(entries)
            except Exception:
                entries = []
        for entry in entries:
            if not entry:
                continue
            try:
                self._custom_parsers.append(CustomParser(entry))
            except Exception as e:
                import logging
                logging.getLogger("nonebot_plugin_parser_lite").warning(
                    f"[ParserLite] CustomParser init skip: {e}")

    def _get_parser(self, parser_cls):
        name = parser_cls.__name__
        if name not in self._parsers:
            self._parsers[name] = parser_cls()
        return self._parsers[name]

    def _ensure_parser_httpx(self, ordered: list) -> None:
        """重建已关闭的解析器 httpx 客户端 (插件重载/terminate 后残留)."""
        from httpx import AsyncClient

        for cls in ordered:
            try:
                parser = self._get_parser(cls)
                h = getattr(parser, "httpx", None)
                if h is not None and getattr(h, "is_closed", False):
                    parser.httpx = AsyncClient(
                        headers=getattr(parser, "headers", None),
                        timeout=getattr(parser, "timeout", 15),
                        follow_redirects=True,
                    )
            except Exception:
                continue

    async def close(self):
        for parser in self._parsers.values():
            try:
                await parser.aclose()
            except Exception:
                pass
        for cp in (self._custom_parsers or []):
            try:
                await cp.aclose()
            except Exception:
                pass
        await up_downloader().aclose()
