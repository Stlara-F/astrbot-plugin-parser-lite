#!/usr/bin/env python3
"""
AstrBot adapter for nonebot-plugin-parser-lite.
PR#205 merged → sokoko-org/main. Runs inside nonebot_plugin_parser_lite/ package.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import traceback
from typing import ClassVar

os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

# AstrBot 插件根目录 → src/ 加入 sys.path (上游包 nonebot_plugin_parser_lite 在里面)
_here = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_here, "src")
if os.path.isdir(_src):
    sys.path.insert(0, _src)
sys.path.insert(0, _here)

from astrbot.api import AstrBotConfig
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

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
from nonebot_plugin_parser_lite.constants import PlatformEnum
from nonebot_plugin_parser_lite.data import (
    AudioContent,
    GraphicContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from nonebot_plugin_parser_lite.download import DOWNLOADER
from nonebot_plugin_parser_lite.parsers.base import BaseParser
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)
CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict[str, ParseResult] = LimitedSizeDict(max_size=50)
_DISABLED_GROUPS_FILE = Path(__file__).parent / "data" / "parser_lite" / "disabled_groups.json"

# ── 配置桥接 ──────────────────────────────────────────────────────────────────
class BridgeConfig:
    _instance: _UpConfig | None = None
    _source: dict | None = None
    _hash: str = ""

    @classmethod
    def configure(cls, _config: dict | None = None, **kwargs):
        data = {**(_config or getattr(cls, "_source", {}) or {}), **kwargs}
        if _config is not None:
            cls._source = _config
        elif kwargs:
            cls._source = data  # AstrBot configure(**self.config) 走此分支
        # features 标签 → plite_* bool 反向映射
        features_list = data.get("features", [])
        if isinstance(features_list, list):
            for k, f in _UpConfig.model_fields.items():
                if f.annotation is bool and k.startswith("plite_"):
                    data[k] = _label(k) in features_list
        valid = {k: v for k, v in data.items() if k in _UpConfig.model_fields}
        # parser_extra 冲突覆盖: 注入到 valid 中 (优先于顶级 plite_ 同名字段)
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
        # 更新模块级 pconfig (兼容 AstrBot 多路径导入方式)
        cfg_mod = _UpConfig.__module__  # e.g. 'config' or 'nonebot_plugin_parser_lite.config'
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
        astrbot_logger.debug(f"[ParserLite] configure: {len(valid)} fields, dirty={h != cls._hash}")

    @classmethod
    def _inject_parser_extra(cls, valid: dict, data: dict):
        """将 parser_extra 嵌套表的值解析后写入 valid (覆盖同名字段冲突)"""
        mapping = _get_parser_extra_mapping()
        extra = data.get("parser_extra", {})
        if not extra or not isinstance(extra, dict):
            return
        for ast_key, (pconfig_field, enum_cls, is_list) in mapping.items():
            val = extra.get(ast_key)
            if val is None:
                continue
            if isinstance(val, str):
                # 单选项: 直接传字符串 "_1080P"
                if not is_list and val in enum_cls.__members__:
                    valid[pconfig_field] = enum_cls[val]
                # 多选项: JSON 数组字符串 "["AVC","AV1"]"
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

# ── 动态特征表: URL关键词 → 解析器名 (O(1) 路由) ─────────────────────────
def _build_feature_table():
    """扫描上游 parser 的 @handle 装饰器注册表, 0 inspect.getsource()"""
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

# ── Per-parser config helpers ──────────────────────────────────────────────
def _load_parsers_config() -> dict:
    try: return (BridgeConfig._source or {}).get("parsers", {})
    except Exception: return {}

def _is_parser_enabled(platform: str) -> bool:
    try:
        cfg = get_config()
        return platform not in [p.name.lower() if hasattr(p, "name") else str(p).lower() for p in (cfg.disabled_platforms if hasattr(cfg, "disabled_platforms") else [])]
    except Exception:
        return True

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
    except Exception: pass
    return {}

# ── ParserLite 编排器 ─────────────────────────────────────────────────────────
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
        # ① 热重载配置 + 代理环境准备
        get_config()
        proxy_url = (BridgeConfig._source or {}).get("plite_http_proxy", "")
        target = self._route_url(url)  # O(1) 特征路由
        proxy_first = _use_proxy_for(target or "")
        # ② 解析器优先级排序: 特征命中排第一
        ordered = list(BaseParser.get_all_subclass())
        if target:
            ordered = [c for c in ordered if c.__name__ == target] + [c for c in ordered if c.__name__ != target]
        # ③ 双路重试 (代理/直连)
        for attempt in (0, 1):
            use_proxy_now = (proxy_first and attempt == 0) or (not proxy_first and attempt == 1)
            if proxy_url:
                if use_proxy_now:
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                        os.environ[k] = str(proxy_url).strip()
                else:
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                        os.environ.pop(k, None)
            if hasattr(DOWNLOADER, "ensure_client"):
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
                        if cookies and pname == "bilibili":
                            ck_str = next(iter(cookies.values())) if cookies else ""
                            if ck_str:
                                BridgeConfig._source = BridgeConfig._source or {}
                                BridgeConfig._source["plite_bili_ck"] = ck_str
                                get_config()
                                try:
                                    object.__setattr__(get_config(), "plite_bili_ck", ck_str)
                                except Exception: pass
                        return await parser.parse(kw, mwp)
                    except Exception as e:
                        astrbot_logger.warning(f"[ParserLite] {parser_cls.__name__} matched but failed: {e}")
                raise ValueError(f"Unsupported URL: {url}")
            except Exception:
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

# ── CustomParser ───────────────────────────────────────────────────────────────
class CustomParser:
    """自定义解析器: 用户通过 WebUI template_list 配置正则提取规则"""

    SCHEMA: ClassVar[list[dict]] = [
        {"key": "_header",   "type": "text",   "desc": "── 基础配置 ──",                      "default": ""},
        {"key": "name",      "type": "string", "desc": "解析器ID (唯一标识)"},
        {"key": "display",   "type": "string", "desc": "显示名"},
        {"key": "url_pattern","type": "string", "desc": "URL匹配正则"},
        {"key": "_extract",  "type": "text",   "desc": "── 内容提取 (留空=跳过) ──",          "default": ""},
        {"key": "title_re",  "type": "string", "desc": "标题正则",       "default": ""},
        {"key": "author_re", "type": "string", "desc": "作者正则",       "default": ""},
        {"key": "image_re",  "type": "string", "desc": "图片正则",       "default": ""},
        {"key": "video_re",  "type": "string", "desc": "视频正则",       "default": ""},
        {"key": "audio_re",  "type": "string", "desc": "音频正则",       "default": ""},
        {"key": "text_re",   "type": "string", "desc": "正文正则",       "default": ""},
        {"key": "cover_re",  "type": "string", "desc": "封面正则",       "default": ""},
        {"key": "timestamp_re","type": "string","desc": "时间戳正则",     "default": ""},
        {"key": "_http",     "type": "text",   "desc": "── HTTP 配置 ──",                      "default": ""},
        {"key": "headers",   "type": "text",   "desc": "请求头(JSON)",   "default": "{}"},
        {"key": "ajax",      "type": "bool",   "desc": "API模式(POST)",  "default": False},
        {"key": "ajax_url",  "type": "string", "desc": "API URL",        "default": ""},
        {"key": "timeout",   "type": "int",    "desc": "超时(秒)",       "default": 30},
        {"key": "encoding",  "type": "string", "desc": "响应编码",       "default": ""},
        {"key": "cookie",    "type": "string", "desc": "Cookie",         "default": ""},
        {"key": "ua",        "type": "string", "desc": "User-Agent",     "default": ""},
        {"key": "referer",   "type": "string", "desc": "Referer",        "default": ""},
        {"key": "_extras",   "type": "text",   "desc": "── 扩展参数 (键值对自由扩展) ──",      "default": "{}"},
        {"key": "extras",    "type": "text",   "desc": "扩展参数(JSON)", "default": "{}",
         "hint": "任意键值对, 注入到请求配置"},
    ]
    """字段声明: key=字段键, type=AstrBot类型, desc=描述, default=默认值 — 注入和 __init__ 共用"""

    # 从 SCHEMA 构建默认值查找表
    _DEFAULTS: ClassVar[dict] = {}
    for _s in SCHEMA:
        if "default" in _s:
            _DEFAULTS[_s["key"]] = _s["default"]

    def __init__(self, entry: dict):
        d = self._DEFAULTS
        self._config = entry
        self._name = str(entry.get("name", d.get("name", ""))).strip() or str(entry.get("display", "custom")).strip()
        self._display = str(entry.get("display", self._name))
        self._url_re = re.compile(str(entry.get("url_pattern", d.get("url_pattern", ""))))
        self._title_re = self._compile_opt(entry, "title_re")
        self._author_re = self._compile_opt(entry, "author_re")
        self._image_re = self._compile_opt(entry, "image_re")
        self._video_re = self._compile_opt(entry, "video_re")
        self._audio_re = self._compile_opt(entry, "audio_re")
        self._text_re = self._compile_opt(entry, "text_re")
        self._cover_re = self._compile_opt(entry, "cover_re")
        self._timestamp_re = self._compile_opt(entry, "timestamp_re")
        self._ajax = bool(entry.get("ajax", d.get("ajax", False)))
        self._ajax_url = str(entry.get("ajax_url", d.get("ajax_url", "")))
        self._timeout = int(entry.get("timeout", d.get("timeout", 30)))
        self._encoding = str(entry.get("encoding", d.get("encoding", "")))
        try:
            hdrs = entry.get("headers", d.get("headers", "{}"))
            self._headers = json.loads(hdrs) if isinstance(hdrs, str) else hdrs
        except Exception:
            self._headers = {}
        cookie = str(entry.get("cookie", d.get("cookie", ""))).strip()
        if cookie: self._headers.setdefault("Cookie", cookie)
        ua = str(entry.get("ua", d.get("ua", ""))).strip()
        if ua: self._headers.setdefault("User-Agent", ua)
        referer = str(entry.get("referer", d.get("referer", ""))).strip()
        if referer: self._headers.setdefault("Referer", referer)
        self._extras = {}
        try:
            raw = entry.get("extras", d.get("extras", "{}"))
            self._extras = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            pass
        if self._extras and isinstance(self._extras, dict):
            for k, v in self._extras.items():
                if k not in self._headers:
                    self._headers[str(k)] = str(v)
        self._closed = False

    @staticmethod
    def _compile_opt(entry: dict, key: str):
        v = str(entry.get(key, "")).strip()
        return re.compile(v) if v else None

    @property
    def platform(self):
        from nonebot_plugin_parser_lite.data import Platform
        return Platform(name=self._name, display_name=self._display)

    def search_url(self, url: str):
        m = self._url_re.search(url)
        if not m: return (None, None)
        return (url, m)

    async def parse(self, keyword: str, searched):
        import datetime

        import httpx
        async with httpx.AsyncClient(headers=self._headers or None, follow_redirects=True) as client:
            if self._ajax and self._ajax_url:
                resp = await client.post(self._ajax_url, json={"url": keyword}, timeout=self._timeout)
            else:
                resp = await client.get(keyword, timeout=self._timeout)
            if self._encoding:
                resp.encoding = self._encoding
            text = resp.text
        result = re.sub(r"<[^>]+>", "", text)

        title = ""
        if self._title_re:
            m = self._title_re.search(result)
            if m: title = m.group(1) if m.lastindex else m.group(0)

        author_name = ""
        if self._author_re:
            m = self._author_re.search(result)
            if m: author_name = m.group(1) if m.lastindex else m.group(0)

        timestr = ""
        if self._timestamp_re:
            m = self._timestamp_re.search(result)
            if m: timestr = m.group(1) if m.lastindex else m.group(0)

        texts = []
        if self._text_re:
            for m in self._text_re.finditer(result):
                t = m.group(1) if m.lastindex else m.group(0)
                if t: texts.append(t)

        from nonebot_plugin_parser_lite.creator import Creator
        from nonebot_plugin_parser_lite.data import (
            Author,
            ParseResult,
            Platform,
            Stats,
        )

        platform_inst = Platform(name=self._name, display_name=self._display)
        author = Author(name=author_name or "未知")

        stats = Stats()
        ts = None
        if timestr:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try: ts = datetime.datetime.strptime(timestr, fmt); break
                except ValueError: pass

        content: list = list(texts)
        if self._image_re:
            for m in self._image_re.finditer(result):
                img_url = m.group(1) if m.lastindex else m.group(0)
                if img_url:
                    try: content.append(Creator.image(img_url))
                    except Exception: pass
        if self._video_re:
            for m in self._video_re.finditer(result):
                vid_url = m.group(1) if m.lastindex else m.group(0)
                if vid_url:
                    try: content.append(Creator.video(vid_url))
                    except Exception: pass
        if self._audio_re:
            for m in self._audio_re.finditer(result):
                aud_url = m.group(1) if m.lastindex else m.group(0)
                if aud_url:
                    try: content.append(Creator.audio(aud_url))
                    except Exception: pass

        return ParseResult(
            platform=platform_inst,
            author=author,
            title=title or keyword,
            content=content,
            stats=stats,
            url=keyword,
            timestamp=ts,
        )

    async def aclose(self):
        self._closed = True

# ── helpers ────────────────────────────────────────────────────────────────────
def _detect_missing_libs() -> str:
    import ctypes
    import ctypes.util
    libs = {"libnspr4.so":"nspr4","libnss3.so":"nss3","libgbm.so.1":"gbm",
            "libasound.so.2":"asound","libxkbcommon.so.0":"xkbcommon"}
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

def _load_disabled_groups() -> set[str]:
    try:
        if _DISABLED_GROUPS_FILE.exists():
            return set(json.loads(_DISABLED_GROUPS_FILE.read_text(encoding="utf-8")))
    except Exception: pass
    return set()

def _save_disabled_groups(data: set[str]) -> None:
    _DISABLED_GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_GROUPS_FILE.write_text(json.dumps(list(data)), encoding="utf-8")

def _label(k: str) -> str:
    s = k.removeprefix("plite_").replace("_", " ")
    if s.startswith("bili "): s = "B站" + s[4:]
    return " ".join(w[0].upper() + w[1:] for w in s.split())

# ── 动态注入 ──────────────────────────────────────────────────────────────────
_PARSER_EXTRA_MAP: dict[str, tuple[str, type, bool]] = {}
"""parser_extra UI key → (Config field name, enum class, is_list)"""

# slider 元数据: 上游模型缺少 ge/le 元数据的字段在这里补 (未来上游加了 Field(ge=...) 后自动消除)
_SLIDER_HINTS: dict[str, dict] = {
    "plite_max_size":            {"min": 10,  "max": 500,  "step": 10},
    "plite_duration_maximum":    {"min": 60,  "max": 3600, "step": 60},
    "plite_max_comments":        {"min": 0,   "max": 20,   "step": 1},
    "plite_max_retries":         {"min": 0,   "max": 5,    "step": 1},
}
"""上游字段缺少 Field(ge=,le=) 的 slider 补丁 — 上游补齐后此处自动失效"""

# ── 发送策略: 从 _send_any 方法中提取支持的类型, 0 hardcode ────────────────
def _get_sendable_types() -> list[str]:
    """从 _send_any 方法体中扫描所有 media_type 分支, 自动生成选项列表 (战未来)"""
    import re as _re
    try:
        src = inspect.getsource(ParserLitePlugin._send_any)
    except Exception:
        return ["card", "image", "video", "audio"]
    types = _re.findall(r'media_type\s*==\s*"(\w+)"', src)
    return sorted(set(types))

_get_sendable_types  # 懒求值函数, 在 _BRIDGE_FIELDS 中使用 lambda 调用

# bridge 语义字段: 不在上游 Config 中的 AstrBot 专属配置项, 声明式注入
_BRIDGE_FIELDS: list[dict] = [
    {
        "path": "parsers.items.proxied",
        "type": "list",
        "desc": "走代理的解析器",
        "items_type": "string",
        "source": lambda: sorted({p.name.lower() for cls in BaseParser.get_all_subclass()
                                  if (p := getattr(cls, "platform", None))}),
    },
    {
        "path": "parsers.items.cookies",
        "type": "string",
        "desc": "Cookie映射(JSON)",
        "default": "{}",
        "hint": '语法: {"平台名":"key1=val1; key2=val2"}。例: {"bilibili":"SESSDATA=xxx; bili_jct=yyy","zhihu":"z_c0=zzz"}。B站Cookie从浏览器F12→Application→Cookies→bilibili.com 复制。各平台独立，以分号分隔键值对',
    },
    {
        "path": "send_strategy",
        "type": "list",
        "desc": "发送策略",
        "default": lambda: _get_sendable_types(),
        "options": lambda: _get_sendable_types(),
    },
]
"""AstrBot 专属字段声明: path=JSON路径, source=动态选项生成器(可选), default/hint/desc=静态元数据"""

def _schema_desc(fname: str) -> str:
    s = fname.removeprefix("plite_").replace("_", " ")
    return " ".join(w[0].upper() + w[1:] for w in s.split())

def _is_enum_field(finfo) -> bool:
    import typing
    ann = finfo.annotation
    if hasattr(ann, "__args__"):
        ann = typing.get_args(ann)[0]
    return hasattr(ann, "__members__")

def _is_str_list_field(finfo) -> bool:
    """list[str] 类型"""
    import typing
    ann = finfo.annotation
    if not hasattr(ann, "__args__"): return False
    args = typing.get_args(ann)
    return len(args) == 1 and args[0] is str

def _extract_slider(finfo) -> dict | None:
    """从 pydantic Field 元数据提取 slider 配置"""
    slider = None
    for meta in getattr(finfo, "metadata", []):
        ge_ = getattr(meta, "ge", None); le_ = getattr(meta, "le", None)
        multiple = getattr(meta, "multiple_of", None)
        if ge_ is not None or le_ is not None:
            slider = {}
            if ge_ is not None: slider["min"] = ge_
            if le_ is not None: slider["max"] = le_
            slider["step"] = multiple if multiple else (
                max(1, (slider["max"] - slider.get("min", 0)) // 20) if "min" in slider and "max" in slider else 1
            )
            break
    return slider

def _build_field_entry(fname: str, finfo, is_new: bool) -> dict | None:
    """从 pydantic 字段信息生成 AstrBot schema 条目 (0 hardcode)"""
    ann = finfo.annotation
    default = finfo.default

    # 转换为 JSON-safe 默认值
    try:
        if default is not None and not isinstance(default, (int, float, str, bool, list, dict)):
            if hasattr(default, "name"):
                default_val = default.name
            elif isinstance(default, list) and default and hasattr(default[0], "name"):
                default_val = [e.name for e in default]
            else:
                default_val = None
        elif isinstance(default, list):
            default_val = list(default)
        else:
            default_val = default
    except Exception:
        default_val = None

    entry = {"description": _schema_desc(fname)}

    # int → slider 或 纯 int
    if ann is int or (hasattr(ann, "__origin__") and ann.__origin__ is int):
        entry["type"] = "int"
        slider = _extract_slider(finfo) or _SLIDER_HINTS.get(fname)
        if slider:
            entry["slider"] = slider
    # str
    elif ann is str or (hasattr(ann, "__origin__") and ann.__origin__ is str):
        entry["type"] = "string"
        if fname == "plite_http_proxy":
            entry["hint"] = "socks5://127.0.0.1:1080"
    # bool → 归入 features (不生成独立条目)
    elif ann is bool or (hasattr(ann, "__origin__") and ann.__origin__ is bool):
        return None
    # list[str]
    elif _is_str_list_field(finfo):
        entry["type"] = "list"
        entry["items"] = {"type": "string"}
    # list[Enum] 或 Enum → parser_extra (不生成独立条目)
    elif _is_enum_field(finfo):
        return None
    # float
    elif ann is float or (hasattr(ann, "__origin__") and ann.__origin__ is float):
        entry["type"] = "float"
    else:
        return None  # 未知类型跳过

    if default_val is not None:
        entry["default"] = default_val
    return entry

def _get_parser_extra_mapping() -> dict:
    if not _PARSER_EXTRA_MAP:
        _inject_dynamic_options_static()
    return dict(_PARSER_EXTRA_MAP)

def _inject_dynamic_options_static():
    """0-hardcode 动态注入: 扫描上游 Config 模型 → 填充 _conf_schema.json"""
    import typing

    from nonebot_plugin_parser_lite.constants import PlatformEnum
    schema_path = Path(__file__).parent / "_conf_schema.json"
    flag_path = Path(__file__).parent / ".injected"
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text("utf-8"))
    has_markers = "__INJECT__" in json.dumps(schema)
    if flag_path.exists() and not has_markers:
        _rebuild_parser_extra_map()
        return
    updated = False
    injected = []

    # 0) custom_parsers 模板: 从 CustomParser.SCHEMA 类属性扫描生成
    cp = schema.setdefault("custom_parsers", {"type": "template_list", "description": "自定义解析器", "templates": {}})
    template = cp.setdefault("templates", {}).setdefault("default", {})
    if not template.get("items"):
        items = {}
        for cm in CustomParser.SCHEMA:
            entry = {"type": cm["type"], "description": cm["desc"]}
            if "default" in cm and cm["default"] is not None:
                entry["default"] = cm["default"]
            if "hint" in cm:
                entry["hint"] = cm["hint"]
            items[cm["key"]] = entry
        template["name"] = "自定义"
        template["items"] = items
        updated = True; injected.append("custom_parsers")

    # 1) features: bool 字段
    bool_fields = sorted(k for k, f in _UpConfig.model_fields.items()
                         if f.annotation is bool and k.startswith("plite_"))
    if schema.get("features", {}).get("options") == ["__INJECT__"] or not schema["features"].get("options"):
        schema["features"]["options"] = [_label(k) for k in bool_fields]
        schema["features"]["default"] = [
            _label(k) for k in bool_fields if _UpConfig.model_fields[k].default is True
        ]
        updated = True; injected.append("features")

    # 2) plite_* 顶级字段: 从上游模型自动生成
    for fname, finfo in _UpConfig.model_fields.items():
        if not fname.startswith("plite_"): continue
        ann = finfo.annotation
        is_bool = ann is bool
        is_enum = _is_enum_field(finfo)
        # bool → features; enum → parser_extra; 其他 → 顶级字段
        if is_bool or is_enum:
            continue
        # 已有有效条目(非 __INJECT__) → 仅同步默认值
        if fname in schema and schema[fname] != ["__INJECT__"] and not isinstance(schema[fname], list):
            default = finfo.default
            if default is not None and not isinstance(default, (int, float, str, bool, list, dict)):
                default = None  # Enum 等不可 JSON 序列化
            if default is not None and "default" in schema[fname]:
                schema[fname]["default"] = default
                updated = True
            continue
        entry = _build_field_entry(fname, finfo, is_new=False)
        if entry:
            schema[fname] = entry
            updated = True; injected.append(fname)

    # 3) bridge 语义字段: 从 _BRIDGE_FIELDS 声明式扫描注入
    schema.setdefault("parsers", {"type": "object", "description": "解析器控制", "items": {}})
    for bf in _BRIDGE_FIELDS:
        parts = bf["path"].split(".")
        obj = schema
        for p in parts[:-1]:
            obj = obj.setdefault(p, {} if p != parts[-2] else (obj.get(p, {}) if isinstance(obj.get(p), dict) else {}))
        last = parts[-1]
        existing = obj.get(last, {})
        needs_inject = (
            isinstance(existing, dict) and existing.get("options") == ["__INJECT__"]
        ) or (not isinstance(existing, dict) or not existing)
        if needs_inject:
            entry = {"type": bf["type"], "description": bf["desc"]}
            dv = bf.get("default")
            entry["default"] = dv() if callable(dv) else (dv if dv is not None else [])
            if "items_type" in bf:
                entry["items"] = {"type": bf["items_type"]}
            if "hint" in bf:
                entry["hint"] = bf["hint"]
            if "source" in bf:
                entry["options"] = bf["source"]()
            if "options" in bf:
                opts = bf["options"]
                entry["options"] = opts() if callable(opts) else opts
            if "default" in bf:
                dv = bf["default"]
                entry["default"] = dv() if callable(dv) else dv
            obj[last] = entry
            updated = True; injected.append(bf["path"])

    # 4) plite_disabled_platforms (必须在 parser_extra 之前, 避免重复注入)
    platforms = sorted({p.name for p in PlatformEnum})
    if schema.get("plite_disabled_platforms", {}).get("options") in (["__INJECT__"], None):
        schema["plite_disabled_platforms"] = {
            "type": "list",
            "description": _schema_desc("plite_disabled_platforms"),
            "options": platforms,
            "default": [],
        }
        updated = True; injected.append("plite_disabled_platforms")

    # 5) parser_extra: 枚举字段 (排除已有顶级 schema 的字段)
    _PARSER_EXTRA_MAP.clear()
    extra = {}
    for fname, finfo in _UpConfig.model_fields.items():
        if not _is_enum_field(finfo): continue
        if fname in schema: continue  # 已有顶级字段, 跳过
        ann = finfo.annotation
        is_list = hasattr(ann, "__args__")
        enum_cls = typing.get_args(ann)[0] if is_list else ann
        short_key = fname.removeprefix("plite_")

        try:
            dv = finfo.default
            if dv is not None and hasattr(dv, "name"):
                fallback = dv.name if not is_list else [dv.name]
            elif isinstance(dv, list) and dv and hasattr(dv[0], "name"):
                fallback = [e.name for e in dv]
            else:
                fallback = [] if is_list else ""
        except Exception:
            fallback = [] if is_list else ""

        _PARSER_EXTRA_MAP[short_key] = (fname, enum_cls, is_list)
        if (not schema.get("parser_extra", {}).get("items") or
            short_key not in schema["parser_extra"].get("items", {})):
            extra[short_key] = {
                "description": _schema_desc(fname),
                "type": "string" if not is_list else "list",
                "options": list(enum_cls.__members__),
                "default": fallback,
                "hint": "",
            }
    if extra:
        schema.setdefault("parser_extra", {"type": "object", "description": "解析器专属扩展", "items": {}})
        schema["parser_extra"]["items"] = {**(schema.get("parser_extra", {}).get("items") or {}), **extra}
        updated = True; injected.append("parser_extra")

    # 6) test_urls 默认值 (从 test/test_parsers._FALLBACK_URLS 动态注入)
    tu = schema.get("test_urls", {})
    if tu.get("default") in ([], None, ["__INJECT__"]):
        try:
            from test.test_parsers import _FALLBACK_URLS as _tufb
        except ImportError:
            _tufb = []
        schema["test_urls"] = {
            "type": "list", "description": "测试URL", "default": list(_tufb),
            "hint": "每行一条URL, 平台自动识别", "items": {"type": "string"},
        }
        updated = True; injected.append("test_urls")

    if updated or not flag_path.exists():
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), "utf-8")
        flag_path.write_text("1")
        astrbot_logger.info(f"[ParserLite] schema injected: {', '.join(injected) if injected else '(defaults sync)'}")

def _rebuild_parser_extra_map():
    """注入跳过时仍重建映射表 (保证运行时值同步可用)"""
    import typing
    _PARSER_EXTRA_MAP.clear()
    for fname, finfo in _UpConfig.model_fields.items():
        if not _is_enum_field(finfo): continue
        schema_path = Path(__file__).parent / "_conf_schema.json"
        schema = json.loads(schema_path.read_text("utf-8")) if schema_path.exists() else {}
        if fname in schema: continue  # 已有顶级字段
        ann = finfo.annotation
        is_list = hasattr(ann, "__args__")
        enum_cls = typing.get_args(ann)[0] if is_list else ann
        short_key = fname.removeprefix("plite_")
        _PARSER_EXTRA_MAP[short_key] = (fname, enum_cls, is_list)

# 模块加载时执行注入 (含 _injected 开关保护)
_inject_dynamic_options_static()

# ── 格式化 ────────────────────────────────────────────────────────────────────
def format_full(result: ParseResult) -> str:
    lines = [
        f"【{result.platform.display_name}】{result.author.name}",
        result.title or "",
    ]
    if result.timestamp:
        lines.append(result.formatted_datetime)
    texts = [t for t in result.content if isinstance(t, str)]
    if texts:
        lines.append("\n" + "\n".join(texts))
    media = []
    for item in result.content:
        if isinstance(item, VideoContent): media.append(f"[{item.display_duration}]")
        elif isinstance(item, ImageContent): media.append("[图]")
        elif isinstance(item, AudioContent): media.append("[音]")
    if media: lines.append("\n" + " ".join(media))
    s = result.stats
    stats = []
    if s.view_count: stats.append(f"播放{s.view_count}")
    if s.like_count: stats.append(f"赞{s.like_count}")
    if s.comment_count: stats.append(f"评论{s.comment_count}")
    if s.share_count: stats.append(f"分享{s.share_count}")
    if s.collect_count: stats.append(f"收藏{s.collect_count}")
    if stats: lines.append("\n" + " | ".join(stats))
    if result.comments:
        lines.append(f"\n--- 评论 (共{len(result.comments)}条) ---")
        for i, c in enumerate(result.comments[:5], 1):
            body = " ".join([x for x in c.content if isinstance(x, str)])[:80]
            lines.append(f"[{i}] {c.author.name}: {body}")
    if result.ai_summary and "cookie 未配置" not in result.ai_summary:
        lines.append(f"\nAI摘要: {result.ai_summary[:500]}")
    return "\n".join(lines)

def format_brief(result: ParseResult) -> str:
    lines = [f"【{result.platform.display_name}】{result.author.name}", result.title or ""]
    s = result.stats
    parts = []
    if s.view_count: parts.append(f"播放{s.view_count}")
    if s.like_count: parts.append(f"赞{s.like_count}")
    if s.comment_count: parts.append(f"评论{s.comment_count}")
    if parts: lines.append(" | ".join(parts))
    return "\n".join(lines)

# ── 懒下载管理器 ──────────────────────────────────────────────────────────────
class LazyManager:
    """上游 LazyManager 的 AstrBot 移植版: per-user session + asyncio.Task 自动超时清理"""

    @dataclass
    class Session:
        result: ParseResult
        url: str
        task: asyncio.Task[None] | None = None
        deadline: float = 0.0

    _sessions: ClassVar[dict[str, "LazyManager.Session"]] = {}

    @classmethod
    def add(cls, key: str, result: "ParseResult", url: str, timeout_sec: float) -> None:
        """创建/刷新懒下载会话, 自动注册超时清理任务"""
        cls.remove(key)
        task = asyncio.create_task(cls._timeout_handler(key, timeout_sec))
        cls._sessions[key] = cls.Session(result=result, url=url, task=task, deadline=time.time() + timeout_sec)

    @classmethod
    def get(cls, key: str) -> "LazyManager.Session | None":
        sess = cls._sessions.get(key)
        if sess and time.time() > sess.deadline:
            cls.remove(key)
            return None
        return sess

    @classmethod
    def remove(cls, key: str) -> None:
        sess = cls._sessions.pop(key, None)
        if sess and sess.task and not sess.task.done():
            sess.task.cancel()

    @classmethod
    async def _timeout_handler(cls, key: str, timeout_sec: float) -> None:
        await asyncio.sleep(timeout_sec)
        cls.remove(key)

    @classmethod
    def cleanup(cls) -> int:
        now = time.time()
        expired = [k for k, v in cls._sessions.items() if v.deadline < now]
        for k in expired:
            cls.remove(k)
        return len(expired)

# ── 卡片缓存 ──────────────────────────────────────────────────────────────────
_CARD_CACHE: dict[str, bytes] = {}

# ── 插件主体 ──────────────────────────────────────────────────────────────────
class ParserLitePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._log_bridge: _LoguruBridge | None = None
        self._parser: ParserLite | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._chromium_task: asyncio.Task[None] | None = None
        self._plugin_start_time: float = time.time()
        self._disabled_groups: set[str] = set()
        self._recently_processed: dict[int, float] = {}

    async def initialize(self) -> None:
        try:
            self._log_bridge = _LoguruBridge()
            self._log_bridge.setFormatter(logging.Formatter("%(name)s | %(message)s"))
            sdk = logging.getLogger("nonebot_plugin_parser_lite")
            sdk.addHandler(self._log_bridge)
            sdk.setLevel(logging.DEBUG)
            astrbot_logger.info("[ParserLite]   日志桥接: OK")

            self._disabled_groups = _load_disabled_groups()

            configure(**self.config)
            cfg = get_config()
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(cfg.data_dir) / "playwright_browsers")

            for d in (cfg.cache_dir, cfg.config_dir, cfg.data_dir):
                await d.mkdir(parents=True, exist_ok=True)
            astrbot_logger.info("[ParserLite]   configure: OK")

            self._parser = ParserLite()
            self._plugin_start_time = time.time()
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._chromium_task = asyncio.create_task(self._auto_ensure_chromium())
            astrbot_logger.info("[ParserLite] ✓ initialize 完成")
        except Exception:
            astrbot_logger.error(f"[ParserLite] ✗ initialize 失败\n{traceback.format_exc()}")

    async def terminate(self) -> None:
        for _task_attr in ("_cleanup_task", "_chromium_task"):
            _task = getattr(self, _task_attr, None)
            if _task:
                _task.cancel()
                try: await _task
                except asyncio.CancelledError: pass
        if self._parser is not None:
            try: await self._parser.close()
            except Exception: pass
        if self._log_bridge:
            try:
                logging.getLogger("nonebot_plugin_parser_lite").removeHandler(self._log_bridge)
            except Exception: pass

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CACHE_INTERVAL)
            await self._do_clean_cache()

    async def _do_clean_cache(self) -> int:
        try:
            count = await CacheManager.clean_expired()
            if count: astrbot_logger.info(f"[ParserLite] 缓存清理: {count} files")
            return count
        except Exception:
            astrbot_logger.error(f"[ParserLite] 缓存清理异常\n{traceback.format_exc()}")
            return 0

    async def _auto_ensure_chromium(self) -> None:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.chromium.launch(headless=True); await b.close()
            astrbot_logger.info("[ParserLite] Chromium 已就绪"); return
        except Exception: pass
        astrbot_logger.info("[ParserLite] Chromium 未安装, 异步安装中...")
        pb = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        for url, name in [("https://npmmirror.com/mirrors/playwright","npmmirror"),
                          ("https://playwright.azureedge.net","Azure")]:
            env = os.environ.copy(); env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
            if pb: env["PLAYWRIGHT_BROWSERS_PATH"] = pb
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium",
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                if proc.returncode == 0:
                    astrbot_logger.info(f"[ParserLite] Chromium 安装完成 ({name})")
                    return
                err = stderr.decode(errors="replace").strip()[-300:]
                astrbot_logger.warning(f"[ParserLite] Chromium 安装失败 ({name}): rc={proc.returncode} {err}")
            except asyncio.TimeoutError:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装超时 ({name})")
            except Exception as e:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装异常 ({name}): {e}")
        astrbot_logger.error("[ParserLite] Chromium 自动安装失败")

    # ── OneBot 适配 ───────────────────────────────────────────────────────────
    def _gid(self, event: AstrMessageEvent) -> str:
        try:
            o = event.unified_msg_origin
            return o.split(":")[-1] if o and ":" in o else "unknown"
        except Exception: return "unknown"

    def _key(self, event: AstrMessageEvent) -> str:
        try: return event.unified_msg_origin or event.get_sender_id()
        except Exception: return event.get_sender_id()

    def _disabled(self, event: AstrMessageEvent) -> bool:
        return self._gid(event) in self._disabled_groups

    def _blacklisted(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in get_config().blacklist_users

    def _clean_lazy(self) -> int:
        return LazyManager.cleanup()

    @staticmethod
    def _url_from_text(event: AstrMessageEvent) -> str | None:
        m = URL_RE.search(event.get_message_str().strip())
        return m.group(0) if m else None

    # ── 全消息类型 URL 抽取管线 ──────────────────────────────────────────────
    @classmethod
    def _extract_urls(cls, event: AstrMessageEvent) -> list[str]:
        urls: list[str] = []

        # 1. 遍历 AstrBot 消息链 (使用 isinstance 检测组件类型)
        try:
            chain = event.get_messages()
        except Exception:
            chain = []
        if not chain:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                chain = getattr(msg_obj, "message", None) or getattr(msg_obj, "message_chain", None) or []
        if not isinstance(chain, list):
            chain = []

        for seg in chain:
            # Json 组件: 直接访问 .data (AstrBot 已解析为 dict)
            if isinstance(seg, Comp.Json):
                url = cls._extract_card_json_url(seg.data)
                if url:
                    urls.append(url)

            # Image 组件
            elif isinstance(seg, Comp.Image):
                pass  # 图片不含 URL

            # 通用 dict / OneBot 原始段
            else:
                seg_data = None
                seg_type = ""
                if isinstance(seg, dict):
                    seg_type = str(seg.get("type", "")).lower()
                    seg_data = seg.get("data", {}) or {}
                elif hasattr(seg, "type"):
                    seg_type = str(getattr(seg, "type", "")).lower()
                    seg_data = getattr(seg, "data", None)

                if seg_type == "text" or "text" in seg_type:
                    t = seg_data.get("text", "") if isinstance(seg_data, dict) else str(seg_data or "")
                    cls._collect_urls(t, urls)
                elif "json" in seg_type or "miniapp" in seg_type:
                    d = seg_data.get("data", "") if isinstance(seg_data, dict) else str(seg_data or "")
                    if isinstance(d, dict): d = json.dumps(d, ensure_ascii=False)
                    if isinstance(d, str) and d:
                        url = cls._extract_card_json_url(d)
                        if url: urls.append(url)
                        cls._collect_urls(d, urls)
                elif "xml" in seg_type:
                    d = seg_data.get("data", "") if isinstance(seg_data, dict) else str(seg_data or "")
                    if isinstance(d, dict): d = json.dumps(d, ensure_ascii=False)
                    cls._extract_xml_urls(d, urls)
                elif "reply" in seg_type:
                    if isinstance(seg_data, dict):
                        rt = seg_data.get("text", "") or seg_data.get("message", "") or ""
                        if isinstance(rt, list):
                            rt = " ".join((s.get("data",{}).get("text","") if isinstance(s,dict) else str(s)) for s in rt)
                        cls._collect_urls(str(rt), urls)
                elif "markdown" in seg_type:
                    d = seg_data.get("data", "") or seg_data.get("content", "")
                    if isinstance(d, dict): d = json.dumps(d, ensure_ascii=False)
                    cls._collect_urls(str(d or ""), urls)
                elif "forward" in seg_type:
                    cls._extract_forward_urls(seg_data or {}, urls)

        # 2. 纯文本兜底
        text = event.get_message_str()
        if text:
            cls._collect_urls(text, urls)

        # 去重
        seen = set()
        result = []
        for u in urls:
            u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

    @staticmethod
    def _extract_card_json_url(data) -> str | None:
        """从 Json 组件 .data 中动态提取 URL (0 hardcode: 递归扫描所有含 url/link 键的值)"""
        try:
            if isinstance(data, str):
                data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        # BFS 递归扫描: 优先匹配命名键 (含 url/link 字样), 兜底全文字符串
        queue: list[tuple] = [(data, "")]  # (obj, parent_key)
        named_urls: list[str] = []         # 键名匹配的 URL
        raw_urls: list[str] = []           # 纯文本扫描到的 URL

        while queue:
            obj, pkey = queue.pop(0)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    queue.append((v, str(k).lower()))
            elif isinstance(obj, list):
                for item in obj:
                    queue.append((item, pkey))
            elif isinstance(obj, str) and len(obj) > 10:
                # 键名含 url/link → 命名匹配 (高优先级)
                if "url" in pkey or "link" in pkey:
                    if obj.startswith("http"):
                        named_urls.append(obj)
                # 全文字符串 URL 扫描 (低优先级, 去图标/CDN)
                elif URL_RE.match(obj):
                    if not any(x in obj.lower() for x in ("icon", "logo", "avatar", "thumbnail", "imageview", ".png", ".jpg", ".ico")):
                        raw_urls.append(obj)

        # 优先级: 命名 URL > 裸 URL
        return (named_urls[0] if named_urls else
                raw_urls[0] if raw_urls else None)

    @staticmethod
    def _collect_urls(text: str, urls: list[str]):
        for m in URL_RE.finditer(text):
            urls.append(m.group(0))

    @staticmethod
    def _extract_json_urls(raw: str | dict, urls: list[str]):
        """从 JSON 卡片 data 中提取嵌套 URL (递归 BFS)"""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        queue = [payload]
        keys = ("url", "jumpUrl", "qqdocurl", "share_url", "jump_url", "link", "action_url",
                "source_url", "redirect_url", "preview_url", "article_url")
        seen_objs = set()
        while queue:
            obj = queue.pop(0)
            if id(obj) in seen_objs: continue
            seen_objs.add(id(obj))
            if isinstance(obj, dict):
                for k in keys:
                    v = obj.get(k, "")
                    if isinstance(v, str) and URL_RE.match(v):
                        urls.append(v)
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        queue.append(v)
            elif isinstance(obj, list):
                queue.extend(obj)

    @staticmethod
    def _extract_xml_urls(raw: str, urls: list[str]):
        """从 XML 卡片中提取 URL"""
        for tag in ("url", "qqdocurl", "jumpUrl", "share_url", "link"):
            for m in re.finditer(rf"""<{tag}>\s*(https?://[^<\s]+)\s*</{tag}>""", raw, re.IGNORECASE):
                urls.append(m.group(1).strip())
        for attr in ("url", "qqdocurl", "jumpUrl", "share_url"):
            for m in re.finditer(rf"""{attr}\s*=\s*['"](https?://[^\s'"<>]+)['"]""", raw, re.IGNORECASE):
                urls.append(m.group(1).strip())

    @staticmethod
    def _extract_forward_urls(seg_data, urls: list[str]):
        """从合并转发节点中递归提取 URL"""
        if isinstance(seg_data, dict):
            msgs = seg_data.get("messages", []) or seg_data.get("content", [])
        elif isinstance(seg_data, str):
            try:
                parsed = json.loads(seg_data)
                msgs = parsed.get("messages", []) if isinstance(parsed, dict) else []
            except (json.JSONDecodeError, TypeError):
                return
        else:
            msgs = seg_data if isinstance(seg_data, list) else []
        if not isinstance(msgs, list):
            return
        for node in msgs:
            if not isinstance(node, dict):
                continue
            # 子消息体 key 可能是 message / content
            sub_msgs = node.get("message", None) or node.get("content", None)
            if isinstance(sub_msgs, list):
                for sub in sub_msgs:
                    if isinstance(sub, dict):
                        sub_data = sub.get("data", {})
                        if isinstance(sub_data, dict):
                            for f in ("text", "content", "url"):
                                v = sub_data.get(f, "")
                                if v:
                                    ParserLitePlugin._collect_urls(str(v), urls)
                # 继续递归 "messages" 内的嵌套
                for sub in sub_msgs:
                    if isinstance(sub, dict) and sub.get("type") in ("forward", "node"):
                        ParserLitePlugin._extract_forward_urls(sub.get("data", {}), urls)
            elif isinstance(sub_msgs, str):
                ParserLitePlugin._collect_urls(sub_msgs, urls)

    # ── 核心逻辑 ──────────────────────────────────────────────────────────────
    async def _parse_raw(self, url: str) -> ParseResult | None:
        if self._parser is None: return None
        if url in _RESULT_CACHE: return _RESULT_CACHE[url]
        try:
            result = await self._parser.parse_url(url)
            _RESULT_CACHE[url] = result
            return result
        except ValueError: return None
        except Exception:
            astrbot_logger.error(f"[ParserLite] _parse_raw 异常\n{traceback.format_exc()}")
            return None

    async def _parse_and_format(self, url: str) -> str:
        result = await self._parse_raw(url)
        return format_full(result) if result else ""

    # ── 多媒体发送管线 (文件系统→base64→URL 三路冗余 + FFmpeg格式转换) ──
    async def _send_any(self, event: AstrMessageEvent, p: Path, media_type: str,
                        source_url: str = "", duration: float = 0.0):
        if not p.exists():
            astrbot_logger.warning(f"[ParserLite] _send_any: file missing {p}")
            return
        try: p.chmod(0o644)
        except Exception: pass

        if media_type == "image":
            # 1) fromFileSystem → 2) raw bytes → 3) fromURL
            try:
                await event.send(event.chain_result([Comp.Image.fromFileSystem(str(p))]))
                astrbot_logger.debug(f"[ParserLite]   image via file: {p.name}")
                return
            except Exception: pass
            try:
                raw = p.read_bytes()
                if len(raw) > 20 * 1024 * 1024:
                    raw = await self._compress_image(p)
                await event.send(event.chain_result([Comp.Image.fromBytes(raw)]))
                astrbot_logger.debug(f"[ParserLite]   image via bytes: {len(raw)}B")
                return
            except Exception: pass
            if source_url:
                try:
                    await event.send(event.chain_result([Comp.Image.fromURL(source_url)]))
                    astrbot_logger.debug("[ParserLite]   image via URL")
                    return
                except Exception: pass

        elif media_type == "video":
            mp4 = await self._convert_video(p)
            sz = mp4.stat().st_size if mp4.exists() else 0
            # 1) fromFileSystem → 2) raw→base64 → 3) fromURL
            try:
                await event.send(event.chain_result([Comp.Video.fromFileSystem(str(mp4))]))
                astrbot_logger.debug(f"[ParserLite]   video via file: {mp4.name} ({sz // 1024}KB)")
                return
            except Exception: pass
            try:
                import base64
                raw = mp4.read_bytes(); b64 = base64.b64encode(raw).decode()
                await event.send(event.chain_result([Comp.Video.fromBase64(b64)]))
                astrbot_logger.debug(f"[ParserLite]   video via base64: {len(raw)}B")
                return
            except Exception: pass
            if source_url:
                try:
                    await event.send(event.chain_result([Comp.Video.fromURL(source_url)]))
                    astrbot_logger.debug("[ParserLite]   video via URL")
                    return
                except Exception: pass

        elif media_type == "audio":
            # 转码为 MP3 (QQ/OneBot 兼容) + AMR 备路 (QQ语音)
            mp3 = await self._convert_audio(p, fmt="mp3")
            sz = mp3.stat().st_size if mp3.exists() else 0
            # 1) fromFileSystem → 2) raw bytes → 3) fromURL
            try:
                await event.send(event.chain_result([Comp.Record.fromFileSystem(str(mp3))]))
                astrbot_logger.debug(f"[ParserLite]   audio via file: {mp3.name} ({sz // 1024}KB)")
                return
            except Exception: pass
            try:
                raw = mp3.read_bytes()
                await event.send(event.chain_result([Comp.Record.fromBytes(raw)]))
                astrbot_logger.debug(f"[ParserLite]   audio via bytes: {len(raw)}B")
                return
            except Exception: pass
            if source_url:
                try:
                    await event.send(event.chain_result([Comp.Record.fromURL(source_url)]))
                    astrbot_logger.debug("[ParserLite]   audio via URL")
                    return
                except Exception: pass
            # 最终兜底: 当群文件发送
            try:
                await event.send(event.chain_result([Comp.File(file=str(mp3))]))
                return
            except Exception: pass

        elif media_type == "card":
            raw = p.read_bytes()
            try:
                await event.send(event.chain_result([Comp.Image.fromFileSystem(str(p))]))
                return
            except Exception: pass
            await event.send(event.chain_result([Comp.Image.fromBytes(raw)]))

    async def _compress_image(self, p: Path) -> bytes:
        """压缩超大图片 (JPEG quality 80%, 最大 20MB)"""
        import io

        from PIL import Image as PILImage
        img = PILImage.open(str(p))
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
        return buf.getvalue()

    async def _convert_audio(self, p: Path, fmt: str = "mp3") -> Path:
        """FFmpeg 音频转码: → MP3 (128k) 或 AMR (8k mono)"""
        if not await FFmpeg.is_available():
            return p
        if p.suffix.lower() in (".mp3", ".m4a", ".aac", ".wav") and fmt == "mp3":
            return p
        out = p.parent / f"{p.stem}_cvt.{fmt}"
        if out.exists(): return out
        opts = ["-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                "-ac", "1", "-ar", "44100", "-b:a", "128k", str(out)] if fmt == "mp3" else \
               ["-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                "-ac", "1", "-ar", "8000", "-b:a", "12.2k", str(out)]
        try:
            await FFmpeg.exec_ffmpeg(opts)
            return out
        except Exception:
            return p

    async def _convert_video(self, p: Path) -> Path:
        """FFmpeg 视频转封装/转码: → H.264 + AAC in MP4"""
        if not await FFmpeg.is_available():
            return p
        if p.suffix.lower() == ".mp4":
            return p
        out = p.parent / f"{p.stem}_cvt.mp4"
        if out.exists(): return out
        try:
            await FFmpeg.exec_ffmpeg([
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(p), "-c:v", "libx264", "-preset", "fast",
                "-crf", "28", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(out),
            ])
            return out
        except Exception:
            return p

    async def _send_card(self, event: AstrMessageEvent, result: ParseResult):
        cache_key = result.url
        if cache_key in _CARD_CACHE:
            data = _CARD_CACHE[cache_key]
            await event.send(event.chain_result([Comp.Image.fromBytes(data)]))
            astrbot_logger.info(f"[ParserLite] card cache hit ({len(data)} bytes)")
            return

        import uuid

        import jinja2

        from nonebot_plugin_parser_lite.render import RENDERER, safe_src
        try:
            from playwright.async_api import async_playwright
            tpl_data = await RENDERER.resolve_parse_result(result)
            env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(RENDERER.templates_dir)), enable_async=True)
            env.filters["safe_src"] = safe_src
            tpl = env.get_template("default.html.jinja")
            html = await tpl.render_async(result=tpl_data, theme="light")
            tmp = Path(str(RENDERER.templates_dir)) / f"_card_{uuid.uuid4().hex}.html"
            tmp.write_text(html, encoding="utf-8")
            try:
                async with async_playwright() as pw:
                    b = await pw.chromium.launch(headless=True)
                    pg = await b.new_page(viewport={"width": 620, "height": 100})
                    await pg.goto(f"file:///{tmp.as_posix()}", wait_until="networkidle")
                    data = await pg.locator("body").screenshot(type="jpeg", quality=85)
                    await b.close()
                if len(data) < 1024 or data[:2] != b"\xff\xd8":
                    raise RuntimeError(f"Invalid JPEG: {len(data)} bytes")
                if len(_CARD_CACHE) >= 10:
                    _CARD_CACHE.clear()
                _CARD_CACHE[cache_key] = data
                await event.send(event.chain_result([Comp.Image.fromBytes(data)]))
                astrbot_logger.info(f"[ParserLite] card rendered ({len(data)} bytes)")
            finally:
                tmp.unlink(missing_ok=True)
        except Exception:
            astrbot_logger.warning(f"[ParserLite] 卡片渲染失败, 回退文本\n{traceback.format_exc()}")
            await event.send(event.chain_result([Comp.Plain(format_full(result))]))

    # ── 自动触发的 URL 解析 ────────────────────────────────────────────────────
    async def on_url_auto(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message_group(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_message_private(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    async def on_message(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    def _should_send(self, media_type: str) -> bool:
        """发送策略门: 从配置读取, 默认从 _send_any 自动扫描的全部类型"""
        try:
            s = (BridgeConfig._source or {}).get("send_strategy", _get_sendable_types())
            if isinstance(s, str):
                try: s = json.loads(s)
                except Exception: s = _get_sendable_types()
            return media_type in (s if isinstance(s, list) else [])
        except Exception:
            return True

    async def _send_items(self, event: AstrMessageEvent, items: list, result: ParseResult):
        """统一发送入口: 超过4项且配置允许 → 合并转发, 否则逐一发送"""
        need_forward = (
            get_config().need_forward_contents
            and len([i for i in items if hasattr(i, "path_task")]) > 4
        )
        if need_forward:
            await self._send_as_forward(event, items, result)
        else:
            for item in items:
                await self._send_one(event, item)

    async def _send_one(self, event: AstrMessageEvent, item):
        """发送单个媒体项"""
        if not hasattr(item, "path_task"): return
        try:
            src_url = getattr(item.path_task, "url", "")
            dur = getattr(item, "duration", 0.0)
            p = Path(str(await item.path_task))
            if isinstance(item, (ImageContent, GraphicContent)):
                if self._should_send("image"):
                    await self._send_any(event, p, "image", source_url=src_url)
            elif isinstance(item, VideoContent):
                if self._should_send("video"):
                    await self._send_any(event, p, "video", source_url=src_url, duration=dur)
            elif isinstance(item, AudioContent):
                if self._should_send("audio"):
                    await self._send_any(event, p, "audio", source_url=src_url, duration=dur)
        except Exception: pass

    async def _send_as_forward(self, event: AstrMessageEvent, items: list, result: ParseResult):
        """合并转发: 将多项媒体内容打包为 Comp.Nodes (移植自上游 Renderer.__build_forward_segs)"""
        nodes = []
        author = result.author.name if result.author and result.author.name else "解析"
        platform = result.platform.display_name if result.platform else ""
        MAX_PER_NODE = 90  # OneBot 限制

        for item in items:
            if not hasattr(item, "path_task"): continue
            if len(nodes) >= MAX_PER_NODE: break
            try:
                p = Path(str(await item.path_task))
                if isinstance(item, ImageContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} | {platform}"),
                                 Comp.Image.fromFileSystem(str(p))],
                        name=author, uin="0"))
                elif isinstance(item, VideoContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} 的视频"),
                                 Comp.Video.fromFileSystem(str(p))],
                        name=author, uin="0"))
                elif isinstance(item, AudioContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} 的音频"),
                                 Comp.Record.fromFileSystem(str(p))],
                        name=author, uin="0"))
            except Exception: pass

        if nodes:
            await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))

    _DEDUP_TTL = 60  # seconds

    async def _handle_card_message(self, event: AstrMessageEvent):
        # 二选一门: 用原始 message_id 去重 (跨 handler 实例, TTL=60s)
        msg_id = None
        # AstrBot aiocqhttp → event.message_obj.raw_message.message_id
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            raw = getattr(msg_obj, "raw_message", None)
            if isinstance(raw, dict):
                msg_id = raw.get("message_id")
        # fallback: event.get_message_str() 取 hash
        if msg_id is None:
            msg_id = hash(event.get_message_str())
        now = time.time()
        if msg_id in self._recently_processed:
            if now - self._recently_processed[msg_id] < self._DEDUP_TTL:
                return
        self._recently_processed[msg_id] = now
        if len(self._recently_processed) > 50:
            cutoff = now - self._DEDUP_TTL
            self._recently_processed = {k: v for k, v in self._recently_processed.items() if v > cutoff}
        urls = self._extract_urls(event)
        if not urls: return
        if self._disabled(event) or self._blacklisted(event): return
        for url in urls[:3]:
            result = await self._parse_raw(url)
            if result is None: continue
            if self._should_send("card"):
                await self._send_card(event, result)
            await self._send_items(event, result.content, result)

    # ── 命令 ──────────────────────────────────────────────────────────────────
    async def cmd_parse(self, event: AstrMessageEvent):
        if self._blacklisted(event) or self._disabled(event):
            yield event.plain_result("本群已禁用"); return
        urls = self._extract_urls(event)
        if not urls:
            yield event.plain_result("未找到链接"); return
        url = urls[0]
        astrbot_logger.info(f"[ParserLite] cmd_parse: {url[:120]}")
        try:
            result = await self._parse_raw(url)
            if result is None:
                yield event.plain_result("不支持的链接"); return
            if self._should_send("card"):
                await self._send_card(event, result)
            await self._send_items(event, result.content, result)
            if result.platform and result.platform.name == "bilibili":
                LazyManager.add(self._key(event), result, result.url,
                                get_config().plite_lazy_download_timeout)
        except Exception as e:
            astrbot_logger.error(f"[ParserLite] cmd_parse 异常\n{traceback.format_exc()}")
            yield event.plain_result(f"解析失败: {e}")

    async def cmd_parse_dl(self, event: AstrMessageEvent):
        urls = self._extract_urls(event)
        if urls:
            async for _ in self.cmd_parse(event): yield _
        else:
            yield event.plain_result("未找到链接")

    async def _on_download_trigger(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        if not re.match(r"^(xz|下载)$", text): return
        key = self._key(event)
        session = LazyManager.get(key)
        if not session:
            yield event.plain_result("没有待下载的链接"); return
        LazyManager.remove(key)
        result = await self._parse_raw(session.url)
        if result is None:
            yield event.plain_result("不支持的链接"); return
        if self._should_send("card"):
            await self._send_card(event, result)
        await self._send_items(event, result.content, result)
        yield event.plain_result("已下载")

    async def cmd_clean(self, event: AstrMessageEvent):
        count = await self._do_clean_cache()
        yield event.plain_result(f"清理完成: {count} files")

    async def cmd_status(self, event: AstrMessageEvent):
        get_config()  # ensure initialized
        uptime = int(time.time() - self._plugin_start_time)
        h, m = divmod(uptime, 3600); m2, s = divmod(m, 60)
        lines = [
            "ParserLite v1.3.1", f"Uptime: {h}h{m2}m{s}s",
            f"Cache: {len(_RESULT_CACHE)} entries",
            f"Disabled groups: {len(self._disabled_groups)}",
            f"Lazy: {len(LazyManager._sessions)} sessions",
            f"Platforms: {len(PlatformEnum)}",
            f"Parsers: {len(list(BaseParser.get_all_subclass()))}",
        ]
        try: lines.append(f"FFmpeg: is_available={FFmpeg.is_available}")
        except Exception as e: lines.append(f"FFmpeg: {e}")
        yield event.plain_result("\n".join(lines))

    async def cmd_enable(self, event: AstrMessageEvent):
        gid = self._gid(event)
        self._disabled_groups.discard(gid)
        _save_disabled_groups(self._disabled_groups)
        yield event.plain_result("本群解析已开启")

    async def cmd_disable(self, event: AstrMessageEvent):
        gid = self._gid(event)
        self._disabled_groups.add(gid)
        _save_disabled_groups(self._disabled_groups)
        yield event.plain_result("本群解析已关闭")

    async def cmd_doctor(self, event: AstrMessageEvent):
        """战未来诊断: 全动态扫描, 0 hardcode, 含错误行号"""
        import traceback as _tb
        lines = ["=== ParserLite Doctor ===", ""]
        todo: list[str] = []
        errlog: list[str] = []  # 汇总所有 FAIL 的堆栈摘要

        def _fail(label: str, exc: Exception):
            tb_lines = _tb.format_exception(exc)
            last = "".join(tb_lines[-3:]) if len(tb_lines) >= 3 else "".join(tb_lines)
            last = last.strip()[-300:]
            lines.append(f"[FAIL] {label}: {exc}")
            errlog.append(f"── {label} ──\n{last}")
            todo.append(label)

        # ── 1. 环境 ──
        try:
            cfg = get_config()
            if cfg is not None:
                lines.append(f"[OK] Config: {len(cfg.model_fields)} fields, cache={cfg.cache_dir}")
            else:
                lines.append("[WARN] Config: plugin not yet initialized (first load)")
        except Exception as e:
            _fail("Config", e)
        try:
            avail = await FFmpeg.is_available()
            lines.append(f"[OK] FFmpeg: {'yes' if avail else 'no (apt install ffmpeg)'}")
        except Exception as e:
            _fail("FFmpeg", e)
        try:
            if hasattr(DOWNLOADER, "ensure_client"):
                DOWNLOADER.ensure_client()
                lines.append("[OK] Downloader: ready")
            else:
                lines.append("[OK] Downloader: loaded (no ensure_client)")
        except Exception as e:
            _fail("Downloader", e)
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.chromium.launch(headless=True); await b.close()
            lines.append("[OK] Chromium: ready")
        except Exception as e:
            s = str(e)[:100]
            lines.append(f"[WARN] Chromium: {s}")
            todo.append("发送 parse_install_chromium 安装浏览器")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("https://httpbin.org/ip")
            lines.append(f"[OK] Network: reachable (HTTP {r.status_code})")
        except Exception as e:
            lines.append(f"[WARN] Network: {str(e)[:100]}")
            todo.append("检查网络/代理配置")

        # ── 2. 模块 ──
        mod_count = len([m for m in sys.modules if "nonebot_plugin_parser_lite" in m])
        lines.append(f"[OK] Modules: {mod_count} loaded")

        # ── 3. 解析器 ──
        parsers = list(BaseParser.get_all_subclass())
        broken = []
        for cls in parsers:
            try:
                cls()  # test instantiation
                if not getattr(cls, "platform", None):
                    broken.append(f"{cls.__name__}: missing platform")
            except Exception as e:
                broken.append(f"{cls.__name__}: {e}")
                _fail(f"Parser {cls.__name__}", e)
        lines.append(f"[OK] Parsers: {len(parsers)} total, {len(parsers)-len(broken)} ok" + (f", {len(broken)} broken" if broken else ""))

        # ── 4. 平台覆盖 ──
        from nonebot_plugin_parser_lite.constants import PlatformEnum
        enum_set = {p.name.lower() for p in PlatformEnum}
        parser_set = set()
        for cls in parsers:
            p = getattr(cls, "platform", None)
            if p: parser_set.add(p.name.lower())
        missing = enum_set - parser_set
        lines.append(f"[OK] Coverage: {len(parser_set)}/{len(enum_set)} platforms")
        if missing:
            lines.append(f"[INFO] Missing: {', '.join(sorted(missing))}")

        # ── 5. URL 测试 ──
        _build_feature_table()
        lines.append(f"[OK] Route table: {len(FEATURE_TABLE)} keywords")
        try:
            from test.test_parsers import _FALLBACK_URLS as _tufb
        except Exception as e:
            _tufb = []; _fail("test_parsers import", e)
        test_urls = list(_tufb)
        source = BridgeConfig._source or {}
        tu = source.get("test_urls", [])
        if isinstance(tu, str):
            try: tu = json.loads(tu)
            except Exception: tu = []
        if tu and isinstance(tu, list): test_urls = [u for u in tu if isinstance(u, str) and u.strip()]
        live_count = 0
        dead_count = 0
        for url in test_urls[:5]:
            matched = False
            for cls in parsers:
                try:
                    kw, _ = cls.search_url(url)
                    if kw:
                        pn = getattr(getattr(cls, "platform", None), "name", "?")
                        lines.append(f"  [OK] {pn}: {url[:60]}")
                        matched = True; live_count += 1; break
                except Exception:
                    continue  # 非目标解析器静默跳过
            if not matched:
                lines.append(f"  [FAIL] no match: {url[:60]}")
                dead_count += 1
        if not live_count:
            todo.append("在 WebUI test_urls 中添加有效测试链接")

        # ── 6. 注入 ──
        flag = Path(__file__).parent / ".injected"
        lines.append(f"[OK] Injected: {'yes' if flag.exists() else 'no (will inject on restart)'}")

        # ── 7. 错误详单 ──
        if errlog:
            lines.append(f"\n── 错误详情 ({len(errlog)} 项) ──")
            for e in errlog:
                lines.append(e)

        # ── 8. 渲染管线 (曾因 helper.py→nonebot 导入崩溃) ──
        try:
            from nonebot_plugin_parser_lite.render import RENDERER
            lines.append(f"[OK] Render: templates={RENDERER.templates_dir}")
        except Exception as e:
            _fail("Render import (helper.py no nonebot guard)", e)
        try:
            import importlib.util
            if importlib.util.find_spec("jinja2") is not None:
                lines.append("[OK] jinja2: available")
            else:
                lines.append("[WARN] jinja2: not installed → card rendering disabled")
                todo.append("pip install jinja2")
        except Exception:
            lines.append("[WARN] jinja2: import check failed")
        if todo:
            lines.append(f"\n── 修复建议 ({len(todo)} 项) ──")
            for i, t in enumerate(todo, 1): lines.append(f"  {i}. {t}")
        yield event.plain_result("\n".join(lines))

    async def cmd_install_chromium(self, event: AstrMessageEvent):
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.chromium.launch(headless=True); await b.close()
            yield event.plain_result("Chromium 已可用, 无需重复安装"); return
        except Exception: pass
        yield event.plain_result("开始安装 Chromium (耗时较长, 请等待)...")
        pb = str(Path(get_config().data_dir) / "playwright_browsers")
        for url, name in [("https://npmmirror.com/mirrors/playwright","npmmirror"),
                          ("https://playwright.azureedge.net","Azure")]:
            env = os.environ.copy(); env["PLAYWRIGHT_BROWSERS_PATH"] = pb
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
            try:
                yield event.plain_result(f"尝试 {name} ({url}) ...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium", env=env)
                await asyncio.wait_for(proc.wait(), timeout=600)
                missing = _detect_missing_libs()
                yield event.plain_result(f"Chromium 安装完成 ({name})!"
                                         + (f"\n缺失系统库: {missing}" if missing else ""))
                return
            except asyncio.TimeoutError:
                yield event.plain_result(f"{name} 超时, 切换镜像...")
            except Exception as e:
                yield event.plain_result(f"{name} 失败: {e}\n切换镜像...")
        yield event.plain_result("所有镜像均失败, 请手动安装")

    async def cmd_bm(self, event: AstrMessageEvent):
        """下载 B站音频: 从当前消息 / 懒下载会话 / 回复消息 三路提取 BV 号"""
        text = event.get_message_str()
        bvid = None

        # 1) 当前消息直接匹配
        m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", text)
        if m: bvid = m.group(0)

        # 2) 懒下载会话中提取 (先回复了 bilibili 链接, 再发 cmd_bm)
        if not bvid:
            session = LazyManager.get(self._key(event))
            if session and session.url:
                m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", session.url)
                if m: bvid = m.group(0)

        # 3) 从被回复的消息中提取 BV (上游 BvReplyMergeExtension 等价实现)
        if not bvid:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                raw_segs = getattr(msg_obj, "message", None) or []
                for seg in (raw_segs if isinstance(raw_segs, list) else []):
                    if isinstance(seg, dict) and seg.get("type") == "reply":
                        reply_data = seg.get("data", {})
                        reply_text = reply_data.get("text", "") or reply_data.get("message", "") or ""
                        m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", str(reply_text))
                        if m:
                            bvid = m.group(0)
                            break

        if not bvid:
            yield event.plain_result("未找到BV号 (当前消息/懒下载会话/回复消息均无)"); return

        from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser
        bili = BilibiliParser()
        try:
            urls = await bili.extract_download_urls(bvid=bvid)
            _video_url, audio_url = (urls[0], urls[1]) if len(urls) > 1 else (urls[0], None)
            if audio_url:
                yield event.plain_result(f"Audio: {audio_url[:80]}")
            else:
                yield event.plain_result("该视频未提取到独立音频流")
        except Exception as e:
            yield event.plain_result(f"Error: {e}")
        finally:
            await bili.aclose()

    async def cmd_blogin(self, event: AstrMessageEvent):
        from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser
        bili = BilibiliParser()
        try:
            qr_bytes = await bili.login_with_qrcode()
            yield event.plain_result("B站登录二维码已生成, 请用手机B站扫描以下二维码:")
            yield event.chain_result([Comp.Image.fromBytes(qr_bytes)])
        except Exception as e:
            yield event.plain_result(f"Error: {e}")

    async def parse_url(self, event: AstrMessageEvent, url: str) -> str:
        if self._blacklisted(event): return "黑名单用户"
        cfg = get_config()
        disabled = cfg.disabled_platforms
        for d in disabled:
            if isinstance(d, str): d_name = d.lower()
            else: d_name = d.name.lower() if hasattr(d, "name") else str(d).lower()
            if d_name:
                for cls in BaseParser.get_all_subclass():
                    p = getattr(cls, "platform", None)
                    if p and p.name.lower() == d_name:
                        return f"{p.display_name} 已禁用"
        result = await self._parse_and_format(url)
        return result or "无法解析该链接"

# ── 装饰器注册 ────────────────────────────────────────────────────────────────
filter.command("parse")(ParserLitePlugin.cmd_parse)
filter.command("parse_dl")(ParserLitePlugin.cmd_parse_dl)
filter.command("parse_clean")(ParserLitePlugin.cmd_clean)
filter.command("parse_status")(ParserLitePlugin.cmd_status)
filter.command("parse_enable")(ParserLitePlugin.cmd_enable)
filter.command("parse_disable")(ParserLitePlugin.cmd_disable)
filter.command("parse_doctor")(ParserLitePlugin.cmd_doctor)
filter.command("parse_install_chromium")(ParserLitePlugin.cmd_install_chromium)
filter.command("cmd_bm")(ParserLitePlugin.cmd_bm)
filter.command("cmd_blogin")(ParserLitePlugin.cmd_blogin)
filter.regex(r"^(xz|下载)$")(ParserLitePlugin._on_download_trigger)
filter.regex(r"https?://")(ParserLitePlugin.on_url_auto)
filter.llm_tool(name="parse_url")(ParserLitePlugin.parse_url)
