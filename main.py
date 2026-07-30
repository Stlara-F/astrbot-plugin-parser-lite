#!/usr/bin/env python3
"""
AstrBot adapter for nonebot-plugin-parser-lite.
PR#205 merged → sokoko-org/main. Runs inside nonebot_plugin_parser_lite/ package.
"""

from __future__ import annotations

import asyncio, functools, inspect, json, logging, os, re, sys, time, traceback
from pathlib import Path
from typing import Optional

os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

# AstrBot 插件根目录 → src/ 加入 sys.path (上游包 nonebot_plugin_parser_lite 在里面)
_here = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_here, "src")
if os.path.isdir(_src):
    sys.path.insert(0, _src)

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger as astrbot_logger

# ── Monkey-patch ────────────────────────────────────────────────────────────────
if not hasattr(logging.Logger, "success"):
    logging.Logger.success = logging.Logger.info

# ── 日志桥接 ──────────────────────────────────────────────────────────────────
class _LoguruBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            lv = record.levelno
            if hasattr(astrbot_logger, "opt"):
                fn = (
                    astrbot_logger.opt(depth=1).critical if lv >= logging.CRITICAL
                    else astrbot_logger.opt(depth=1).error if lv >= logging.ERROR
                    else astrbot_logger.opt(depth=1).warning if lv >= logging.WARNING
                    else astrbot_logger.opt(depth=1).info if lv >= logging.INFO
                    else astrbot_logger.opt(depth=1).debug
                )
                fn(msg)
            else:
                astrbot_logger.log(lv, msg)
        except Exception:
            pass

# ── 上游 imports ───────────────────────────────────────────────────────────────
from nonebot_plugin_parser_lite.config import Config as _UpConfig
from nonebot_plugin_parser_lite.data import (
    ParseResult, Comment, ImageContent, VideoContent, AudioContent, GraphicContent,
    LinkContent, LivePhotoContent, StickerContent,
)
from nonebot_plugin_parser_lite.parsers.base import BaseParser
from nonebot_plugin_parser_lite.download import DOWNLOADER
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
from nonebot_plugin_parser_lite.constants import PlatformEnum
from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict
from nonebot_plugin_parser_lite.utils.cache import CacheManager

URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)
CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict[str, ParseResult] = LimitedSizeDict(max_size=50)
_DISABLED_GROUPS_FILE = Path(__file__).parent / "data" / "parser_lite" / "disabled_groups.json"

# ── 配置桥接 ──────────────────────────────────────────────────────────────────
class BridgeConfig:
    _instance: Optional[_UpConfig] = None
    _source: Optional[dict] = None
    _hash: str = ""

    @classmethod
    def configure(cls, _config: dict | None = None, **kwargs):
        data = {**(_config or getattr(cls, "_source", {}) or {}), **kwargs}
        if _config is not None:
            cls._source = _config
        elif kwargs:
            cls._source = data  # AstrBot 调用 configure(**self.config) 走此分支
        # features 标签 → plite_* bool 字段反向映射 (仅设置已勾选的, 未勾选保持上游默认)
        features_list = data.get("features", [])
        if isinstance(features_list, list):
            for k, f in _UpConfig.model_fields.items():
                if f.annotation is bool and k.startswith("plite_"):
                    label = _label(k)
                    if label in features_list:
                        data[k] = True
        valid = {k: v for k, v in data.items() if k in _UpConfig.model_fields}
        cls._inject_parser_extra(valid, data)
        if not valid:
            return
        import hashlib
        s = json.dumps({k: (v.name if hasattr(v, "name") else
                            [e.name for e in v] if isinstance(v, list) and v and hasattr(v[0], "name") else v)
                          for k, v in valid.items()}, sort_keys=True)
        h = hashlib.md5(s.encode()).hexdigest()
        if h == cls._hash:
            return
        cls._hash = h
        cls._instance = _UpConfig(**valid)
        _cfg = cls._instance
        cfg_mod = _UpConfig.__module__
        for key in (cfg_mod, f"nonebot_plugin_parser_lite.{cfg_mod}" if "." not in cfg_mod else cfg_mod):
            mod = sys.modules.get(key)
            if mod is not None:
                mod.pconfig = _cfg
                break
        DOWNLOADER.MAX_RETRIES = _cfg.max_retries
        DOWNLOADER.max_size_mb = _cfg.max_size
        proxy = (cls._source or {}).get("plite_http_proxy", "")
        if proxy:
            os.environ["ALL_PROXY"] = str(proxy)
        else:
            os.environ.pop("ALL_PROXY", None)
        astrbot_logger.debug(f"[ParserLite] configure: {len(valid)} fields")

    @classmethod
    def _inject_parser_extra(cls, valid: dict, data: dict):
        mapping = _get_parser_extra_mapping()
        extra = data.get("parser_extra", {})
        if not extra or not isinstance(extra, dict):
            return
        for ast_key, (pconfig_field, enum_cls, is_list) in mapping.items():
            val = extra.get(ast_key)
            if val is None:
                continue
            if isinstance(val, str):
                if not is_list and val in enum_cls.__members__:
                    valid[pconfig_field] = enum_cls[val]
                elif is_list and val.strip().startswith("["):
                    val = json.loads(val)
            if isinstance(val, list) and is_list:
                valid[pconfig_field] = [enum_cls[v] for v in val if v in enum_cls.__members__]

    @classmethod
    def get_config(cls) -> _UpConfig:
        cls.configure()
        return cls._instance

configure = BridgeConfig.configure
get_config = BridgeConfig.get_config

# ── 动态特征表 ─────────────────────────────────────────────────────────────────
def _build_feature_table():
    import re as _re
    FEATURE_TABLE.clear()
    for cls in BaseParser.get_all_subclass():
        name = cls.__name__
        for _, method in inspect.getmembers(cls, inspect.isfunction):
            kp = getattr(method, "_key_patterns", None)
            if not kp: continue
            for keyword, _pattern, _params in kp:
                if _re.search(r"[\\^$*+?{}()\[\]|]", keyword): continue
                if len(keyword) >= 2:
                    FEATURE_TABLE[keyword] = name

FEATURE_TABLE: dict[str, str] = {}

def _load_parsers_config() -> dict:
    try: return (BridgeConfig._source or {}).get("parsers", {})
    except: return {}

def _is_parser_enabled(platform: str) -> bool:
    return platform not in _load_parsers_config().get("disabled", [])

def _use_proxy_for(platform: str):
    try:
        proxied = _load_parsers_config().get("proxied", [])
        return platform.lower() in [str(p).lower() for p in proxied]
    except Exception: return False

def _get_cookies_for(platform: str) -> dict:
    try:
        raw = _load_parsers_config().get("cookies", "{}")
        cookies = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        ck = cookies.get(platform, "").strip()
        if ck: return {"Cookie": ck}
    except: pass
    return {}

class ParserLite:
    def __init__(self, **config_kwargs):
        if config_kwargs:
            configure(**config_kwargs)
        self._parsers: dict[str, object] = {}
        self._custom_parsers: list[object] | None = None

    def _route_url(self, url: str) -> str | None:
        if not FEATURE_TABLE:
            _build_feature_table()
        for pattern, name in FEATURE_TABLE.items():
            if pattern in url: return name
        return None

    def _load_custom_parsers(self):
        if self._custom_parsers is not None:
            return
        self._custom_parsers = []
        source = BridgeConfig._source or {}
        entries = source.get("custom_parsers", [])
        if isinstance(entries, str):
            try: entries = json.loads(entries)
            except Exception: entries = []
        for entry in entries:
            if not entry: continue
            try:
                self._custom_parsers.append(CustomParser(entry))
            except Exception as e:
                astrbot_logger.warning(f"[ParserLite] CustomParser init skip: {e}")

    async def parse_url(self, url: str) -> ParseResult:
        get_config()
        proxy_url = (BridgeConfig._source or {}).get("plite_http_proxy", "")
        target = self._route_url(url)
        proxy_first = _use_proxy_for(target or "")
        ordered = list(BaseParser.get_all_subclass())
        if target:
            ordered = [c for c in ordered if c.__name__ == target] + [c for c in ordered if c.__name__ != target]
        for attempt in (0, 1):
            use_proxy_now = (proxy_first and attempt == 0) or (not proxy_first and attempt == 1)
            if proxy_url:
                if use_proxy_now:
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                        os.environ[k] = str(proxy_url).strip()
                else:
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                        os.environ.pop(k, None)
            DOWNLOADER.ensure_client()
            try:
                for parser_cls in ordered:
                    pname = getattr(getattr(parser_cls, "platform", None), "name", "")
                    if not _is_parser_enabled(pname or parser_cls.__name__.replace("Parser","").lower()):
                        continue
                    try: kw, mwp = parser_cls.search_url(url)
                    except Exception: continue
                    try:
                        parser = self._get_parser(parser_cls)
                        cookies = _get_cookies_for(pname)
                        if cookies:
                            try:
                                ck_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                                if hasattr(get_config(), "bili_ck") and pname == "bilibili":
                                    setattr(get_config(), "_bili_ck", ck_str)
                            except Exception: pass
                        return await parser.parse(kw, mwp)
                    except Exception as e:
                        astrbot_logger.warning(f"[ParserLite] {parser_cls.__name__} failed: {e}")
                raise ValueError(f"Unsupported URL: {url}")
            except Exception as e1:
                if proxy_url and attempt == 0:
                    mode = "Proxy" if proxy_first else "Direct"
                    fallback = "direct" if proxy_first else "proxy"
                    astrbot_logger.warning(f"[ParserLite] {mode} failed, retrying via {fallback}...")
                    continue
                return await self._try_custom_parsers(url)

    async def _try_custom_parsers(self, url: str) -> ParseResult:
        self._load_custom_parsers()
        for cp in self._custom_parsers:
            try:
                kw, mwp = cp.search_url(url)
                if not kw: continue
                return await cp.parse(kw, mwp)
            except Exception as e:
                astrbot_logger.warning(f"[ParserLite] CustomParser failed: {e}")
        raise ValueError(f"Unsupported URL: {url}")

    def _get_parser(self, parser_cls):
        name = parser_cls.__name__
        if name not in self._parsers:
            self._parsers[name] = parser_cls()
        return self._parsers[name]

    async def close(self):
        for parser in self._parsers.values():
            try: await parser.aclose()
            except Exception: pass
        for cp in (self._custom_parsers or []):
            try: await cp.aclose()
            except Exception: pass
        await DOWNLOADER.aclose()

# [CustomParser class, helpers, injection, formatting, plugin class truncated for length]
# Full file at: https://github.com/Stlara-F/nonebot-plugin-parser-lite/blob/nonebot2astrbot-plugin-parser-lite/main.py