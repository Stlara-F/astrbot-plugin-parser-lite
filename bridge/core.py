"""Bridge core: proxy 解析, BridgeConfig, ParserLite 编排, CustomParser, LazyManager.

与 main.py 的 Star 层解耦 - 纯逻辑模块, 可独立测试.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import ClassVar


def _get_logger():
    """惰性获取日志器 — 无 astrbot 环境 (CI/离线测试) 时回退标准 logging."""
    try:
        from astrbot.api import logger as _l

        return _l
    except Exception:
        import logging

        return logging.getLogger("parser-lite.bridge.core")


astrbot_logger = _get_logger()

from nonebot_plugin_parser_lite.config import Config as _UpConfig
from nonebot_plugin_parser_lite.data import ParseResult
from nonebot_plugin_parser_lite.download import DOWNLOADER
from nonebot_plugin_parser_lite.parsers import load_all as _load_all_parsers
from nonebot_plugin_parser_lite.parsers.base import BaseParser
from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict

_load_all_parsers()  # 新版 parsers 惰性发现 → 显式注册全部平台解析器

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONF_SCHEMA_PATH = Path(_HERE).parent / "_conf_schema.json"
_DISABLED_GROUPS_FILE = Path(_HERE).parent / "data" / "parser_lite" / "disabled_groups.json"

def _label(k: str) -> str:
    s = k.removeprefix("plite_").replace("_", " ")
    if s.startswith("bili "): s = "B站" + s[4:]
    return " ".join(w[0].upper() + w[1:] for w in s.split())


def _resolve_proxy_url(raw: str) -> str:
    """从用户输入解析代理 URL, 支持:
    - 完整协议: http://, https://, socks4://, socks4a://, socks5://, socks5h://
    - 关键词: socks/socks4/socks4a/socks5/socks5h ip:port → protocol://ip:port
    - 裸地址: ip:port → 原样返回 (由调用方通过 _PROXY_PROTOCOLS 自动匹配)
    """
    raw = raw.strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    _lower = raw.lower()
    for _kw in ("socks5h ", "socks5 ", "socks4a ", "socks4 ", "socks "):
        if _lower.startswith(_kw):
            _proto = _kw.strip()
            if _proto == "socks":
                _proto = "socks5"
            return f"{_proto}://{raw[len(_kw):].strip()}"
    # 裸地址：不添加默认协议，留给调用方自动匹配
    return raw


# 代理协议轮询列表 (curl_cffi 全支持, httpx 需 httpx[socks])
_PROXY_PROTOCOLS = ("http://", "https://", "socks5://", "socks5h://")


def _resolve_raw_addr(raw: str) -> str:
    """从原始地址提取 host:port (剥离已存在的协议前缀)"""
    raw = raw.strip()
    for _pfx in ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://"):
        if raw.lower().startswith(_pfx):
            return raw[len(_pfx):]
    return raw


_schema_proxy_cache: str | None = None

def _read_proxy_config() -> str:
    """读取代理配置并输出诊断日志"""
    global _schema_proxy_cache
    _sources = {}
    px = (BridgeConfig._source or {}).get("plite_http_proxy", "")
    if px:
        _sources["_source"] = str(px)[:100]
    else:
        try:
            _sf = json.loads(_CONF_SCHEMA_PATH.read_text("utf-8"))
            _raw = _sf.get("plite_http_proxy")
            if _raw:
                px = _extract_config_value(_raw)
                _schema_proxy_cache = px
                _sources[f"schema({type(_raw).__name__})"] = str(px)[:100]
        except Exception as e:
            _sources["schema_error"] = str(e)[:80]
    if not px and not _sources:
        _sources["_source"] = "(empty)"
    _log_line = ", ".join(f"{k}={v}" for k, v in _sources.items()) if _sources else "not found"
    astrbot_logger.info(f"[ParserLite] proxy config: pyx={px or '<not set>'}, sources: {_log_line}")
    return px


def _extract_config_value(entry) -> str:
    """从 schema entry 中提取用户值 (dict 取 value/default, 否则取裸值)"""
    if isinstance(entry, dict):
        return str(entry.get("value", entry.get("default", "")) or "")
    if isinstance(entry, str):
        return entry
    return ""


_last_proxy: str | None = None

def _apply_downloader_proxy(proxy_url: str):
    """将代理注入 DOWNLOADER 的 httpx/curl_cffi 客户端"""
    global _last_proxy
    proxy_url = _resolve_proxy_url(proxy_url)
    if proxy_url == (_last_proxy or ""):
        return
    _last_proxy = proxy_url
    from curl_cffi import AsyncSession as CurlSession
    from httpx import AsyncClient as HttpxClient
    from httpx import Timeout
    client = DOWNLOADER.client
    # 调度旧客户端关闭 (同次解析中可能轮询多个协议, 旧客户端需释放)
    for _old_attr in ("_httpx", "_curl"):
        _old = getattr(client, _old_attr, None)
        if _old is not None:
            try:
                import asyncio as _asyncio
                _asyncio.get_running_loop().create_task(
                    _old.aclose() if hasattr(_old, "aclose") else _old.close())
            except RuntimeError:
                pass  # 无运行中的 event loop (模块加载时)
    if not proxy_url:
        client._httpx = HttpxClient(verify=False, follow_redirects=True,
                                     timeout=Timeout(timeout=15))
        client._curl = CurlSession(impersonate="chrome146", timeout=240,
                                    verify=False, allow_redirects=True)
    else:
        # 裸地址默认 http:// (parse_url 通过 _PROXY_PROTOCOLS 轮询时会自行加协议)
        _p = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
        client._httpx = HttpxClient(proxy=_p, verify=False,
                                     follow_redirects=True, timeout=Timeout(timeout=15))
        client._curl = CurlSession(proxies={"http": _p, "https": _p},
                                    impersonate="chrome146",
                                    timeout=240, verify=False, allow_redirects=True)
    astrbot_logger.info(f"[ParserLite] downloader proxy: {proxy_url or 'disabled'}")
from nonebot_plugin_parser_lite.parsers import load_all as _load_all_parsers

_load_all_parsers()  # 新版 parsers 惰性发现 → 显式注册全部平台解析器

# ── bridge 模块 (拆分解耦) ─────────────────────────────────────────────────

CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict[str, ParseResult] = LimitedSizeDict(max_size=50)
_DISABLED_GROUPS_FILE = Path(__file__).parent / "data" / "parser_lite" / "disabled_groups.json"
_CONF_SCHEMA_PATH = Path(__file__).parent / "_conf_schema.json"

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
        # 使用官方 configure(): 原地 setattr 更新共享 pconfig 实例,
        # 保持各模块 import 的 pconfig 引用一致性 (不再替换模块属性)
        from nonebot_plugin_parser_lite.config import configure as _up_configure
        try:
            _cfg = _up_configure(_UpConfig(**valid))
        except Exception:
            _cfg = _UpConfig(**valid)
            cfg_mod = _UpConfig.__module__
            for key in (cfg_mod, f"nonebot_plugin_parser_lite.{cfg_mod}" if "." not in cfg_mod else cfg_mod):
                mod = sys.modules.get(key)
                if mod is not None:
                    mod.pconfig = _cfg
                    break
        cls._instance = _cfg
        DOWNLOADER.MAX_RETRIES = _cfg.max_retries
        if hasattr(DOWNLOADER, "max_size_mb"):
            DOWNLOADER.max_size_mb = _cfg.max_size
        _apply_downloader_proxy(_read_proxy_config())
        astrbot_logger.debug(f"[ParserLite] configure: {len(valid)} fields, dirty={h != cls._hash}")

    @classmethod
    def _inject_parser_extra(cls, valid: dict, data: dict):
        """将 parser_extra 嵌套表的值解析后写入 valid (覆盖同名字段冲突)"""
        try:
            from main import _get_parser_extra_mapping  # 延迟 import 解耦 schema 层
            mapping = _get_parser_extra_mapping()
        except Exception:
            mapping = {}  # 无 main 环境 (离线测试/CI) 无 parser_extra 配置
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

def _platform_cfg(platform: str) -> dict:
    """F9: 每平台独立配置 (platforms template_list), 缺失时返回空."""
    try:
        pfm = (BridgeConfig._source or {}).get("platforms", {}) or {}
        if isinstance(pfm, dict):
            return pfm.get(platform, {}) or {}
    except Exception:
        pass
    return {}

def _is_parser_enabled(platform: str) -> bool:
    try:
        # F9: platforms.enable 优先
        _pc = _platform_cfg(platform)
        if "enable" in _pc:
            return bool(_pc["enable"])
        cfg = get_config()
        return platform not in [p.name.lower() if hasattr(p, "name") else str(p).lower() for p in (cfg.disabled_platforms if hasattr(cfg, "disabled_platforms") else [])]
    except Exception:
        return True

def _use_proxy_for(platform: str):
    try:
        # F9: platforms.use_proxy 优先
        _pc = _platform_cfg(platform)
        if "use_proxy" in _pc:
            return bool(_pc["use_proxy"])
        proxied = _load_parsers_config().get("proxied", [])
        return platform.lower() in [str(p).lower() for p in proxied]
    except Exception: return False

def _get_cookies_for(platform: str) -> dict:
    try:
        # F9: platforms.cookies 优先
        _pc = _platform_cfg(platform)
        _ck = (_pc.get("cookies", "") or "").strip()
        if _ck:
            return {"Cookie": _ck}
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

    async def _try_all_parsers(self, ordered, url: str) -> ParseResult:
        _matched_err: Exception | None = None
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
                _matched_err = e
                astrbot_logger.warning(f"[ParserLite] {parser_cls.__name__} matched but failed: {e}")
        if _matched_err is not None:
            raise _matched_err
        raise ValueError(f"Unsupported URL: {url}")

    async def parse_url(self, url: str) -> ParseResult:
        # ① 热重载配置 + 代理环境准备
        get_config()
        proxy_url = _read_proxy_config()
        target = self._route_url(url)  # O(1) 特征路由
        # ② 解析器优先级排序: 特征命中排第一
        ordered = list(BaseParser.get_all_subclass())
        if target:
            ordered = [c for c in ordered if c.__name__ == target] + [c for c in ordered if c.__name__ != target]
        # ③ 代理/直连: proxy 已配置则始终使用 (媒体下载也需要代理, 不能双路切换)
        if proxy_url:
            _try_protocols = _PROXY_PROTOCOLS if "://" not in proxy_url else (proxy_url,)
            _last_err = None
            for _proto in _try_protocols:
                _px = _proto + _resolve_raw_addr(proxy_url) if "://" not in proxy_url else proxy_url
                _apply_downloader_proxy(_px)
                try:
                    return await self._try_all_parsers(ordered, url)
                except Exception as _e:
                    _last_err = _e
                    astrbot_logger.debug(f"[ParserLite] proxy {_px} failed: {_e}")
                    continue
            if _last_err is not None:
                raise _last_err  # type: ignore[misc]
        else:
            try:
                return await self._try_all_parsers(ordered, url)
            except Exception:
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
