#!/usr/bin/env python3
"""
AstrBot adapter for nonebot-plugin-parser-lite.
PR#205 merged → sokoko-org/main. Runs inside nonebot_plugin_parser_lite/ package.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import traceback

os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

# ── 防重复导入守卫 ──────────────────────────────────────────────────────────
# 若本文件已被以另一模块名加载 (如顶层 `main` 与 `data.plugins.X.main` 并存),
# 再次执行模块体会重复注册全部指令 → WebUI 指令冲突 (cmd_blogin 等).
# 检测到重复时直接拒绝, 只保留首次加载的注册集.
_PL_THIS_FILE = os.path.abspath(__file__)
_PL_DUPLICATE_IMPORTS = [
    m.__name__
    for m in list(sys.modules.values())
    if m is not sys.modules.get(__name__)
    and getattr(m, "__file__", None)
    and os.path.abspath(m.__file__) == _PL_THIS_FILE
]
if _PL_DUPLICATE_IMPORTS:
    raise ImportError(
        "[ParserLite] main.py 已被重复加载: 首次加载于 "
        f"{_PL_DUPLICATE_IMPORTS[0]!r}, 本次加载于 {__name__!r}. "
        "为防指令重复注册冲突, 拒绝二次注册. 请检查 AstrBot 插件目录是否存在多个副本. "
    )

# AstrBot 插件根目录 → src/ 加入 sys.path (上游包 nonebot_plugin_parser_lite 在里面)
_here = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_here, "src")
if os.path.isdir(_src):
    sys.path.insert(0, _src)
sys.path.insert(0, _here)

# 新版 standalone: 数据目录指向插件目录下 data/ (避免散落 cwd/.parser-lite)
os.environ.setdefault("PARSER_LITE_BASE_DIR", os.path.join(_here, "data"))

# 清除上游模块缓存 — 多插件目录并存时防止从旧目录加载过期模块
for _mod in list(sys.modules):
    if _mod.startswith("nonebot_plugin_parser_lite"):
        del sys.modules[_mod]

from astrbot.api import AstrBotConfig
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

# ── bridge core (拆分) ─────────────────────────────────────────────────────
from bridge.core import (
    BridgeConfig,
    CustomParser,
    LazyManager,
    ParserLite,
    _detect_missing_libs,
    _load_disabled_groups,
    _save_disabled_groups,
    configure,
    get_config,
)

_CONF_SCHEMA_PATH = Path(__file__).parent / "_conf_schema.json"


# ── 兼容 re-export (从 bridge.core) — 保持外部 API / 测试稳定 ──
from bridge.core import (  # noqa: F401
    _PROXY_PROTOCOLS,
    _apply_downloader_proxy,
    _get_cookies_for,
    _is_parser_enabled,
    _label,
    _load_parsers_config,
    _read_proxy_config,
    _resolve_proxy_url,
    _try_load,
    _use_proxy_for,
)

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
                # 无 opt() 时回退标准 logging (避免 astrbot_logger.log 递归触发 emit)
                _std = logging.getLogger("parser-lite.bridge")
                _std.log(lv, msg)
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
    StickerContent,
    VideoContent,
)
from nonebot_plugin_parser_lite.parsers.base import BaseParser
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict

CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict[str, ParseResult] = LimitedSizeDict(max_size=50)
_CARD_CACHE: dict[str, bytes] = {}
_CARD_CACHE_MAX = 20  # LRU 上限 (动态可调)
from bridge.format import format_full
from bridge.url_extract import (
    collect_urls,
    extract_card_json_url,
    extract_urls,
    url_from_text,
)
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

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
# 排序: 修改频率从高到低 (Cookie/代理 → 平台路由 → 发送 → 阈值 → 开关 → 后台任务)
_BRIDGE_FIELDS: list[dict] = [
{
        "path": "parsers.items.cookies",
        "type": "template_list",
        "desc": "平台Cookie(可增删)",
        "default": [],
        "templates": {
            "default": {
                "name": "Cookie",
                "items": {
                    "platform": {"type": "string", "description": "平台名", "default": ""},
                    "cookie": {"type": "string", "description": "Cookie值", "default": ""},
                },
            },
        },
        "hint": "可增删条目: 每平台一条。例: 平台=bilibili, Cookie=SESSDATA=xxx; bili_jct=yyy",
    },
{
        "path": "plite_http_proxy",
        "type": "string",
        "desc": "HTTP代理",
        "default": "",
        "hint": "全局HTTP/HTTPS代理地址。例: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080。留空则不使用代理。配置后所有解析器请求均通过代理",
    },
{
        "path": "parsers.items.proxied",
        "type": "list",
        "desc": "走代理的解析器",
        "items_type": "string",
        "source": lambda: sorted({p.name.lower() for cls in BaseParser.get_all_subclass()
                                  if (p := getattr(cls, "platform", None))}),
    },
{
        "path": "send_strategy",
        "type": "list",
        "desc": "发送策略",
        "default": lambda: _get_sendable_types(),
        "options": lambda: _get_sendable_types(),
    },
{
        "path": "plite_direct_link",
        "type": "bool",
        "desc": "直链免下载模式",
        "default": False,
        "hint": "开启后视频/图片优先以 URL 直链发送(HEAD+Range探测大小), 不落盘。超限或失败自动回退下载",
    },
{
        "path": "plite_send_cover_only",
        "type": "bool",
        "desc": "视频仅发封面",
        "default": False,
        "hint": "开启后视频只发送 ffmpeg 截取的封面图, 不发送视频本体(省流量)",
    },
{
        "path": "plite_image_compress_mb",
        "type": "int",
        "desc": "图片压缩阈值(MB)",
        "default": 20,
        "hint": "超过此大小的图片自动压缩后再发送",
    },
{
        "path": "plite_dedup_ttl",
        "type": "int",
        "desc": "消息去重窗口(秒)",
        "default": 60,
        "hint": "同一消息在窗口内不重复解析",
    },
{
        "path": "plite_cache_interval",
        "type": "int",
        "desc": "缓存清理间隔(小时)",
        "default": 24,
        "hint": "定期清理过期缓存文件的时间间隔",
    },
{
        "path": "plite_forward_max_nodes",
        "type": "int",
        "desc": "合并转发节点上限",
        "default": 90,
        "hint": "OneBot 合并转发单条消息最大节点数",
    },
{
        "path": "card_semantic",
        "type": "bool",
        "desc": "卡片语义注入(LLM)",
        "default": True,
        "hint": "将QQ分享卡片转为结构化文本注入消息, 供AI助手理解",
    },
{
    "path": "push",
    "type": "template_list",
    "desc": "B站UP订阅推送",
    "default": [],
    "templates": {
        "default": {
            "name": "订阅",
            "items": {
                "uid": {"type": "string", "description": "UP主UID", "default": ""},
                "groups": {"type": "string", "description": "推送群号(逗号分隔)", "default": ""},
                "enabled": {"type": "bool", "description": "启用", "default": True},
            },
        },
    },
    "hint": "可增删订阅条目: 每个UP主一条, 填UID和推送群号",
},
{
    "path": "push_interval",
    "type": "int",
    "desc": "推送轮询间隔(秒)",
    "default": 300,
    "hint": "UP动态/直播轮询间隔",
},
{
    "path": "delay_send",
    "type": "object",
    "items": {
        "enabled": {"type": "bool", "description": "启用延迟发送", "default": False},
        "threshold_mb": {"type": "int", "description": "触发延迟的阈值(MB)", "default": 20},
        "timeout_sec": {"type": "int", "description": "等待超时(秒)", "default": 300},
        "emoji_ids": {"type": "list", "description": "触发表情ID", "items": {"type": "string"}, "default": ["128077"]},
    },
    "desc": "延迟发送(表情触发)",
    "default": {},
    "hint": "大视频先发提示, 回应表情后发送",
},
{
        "path": "arbiter",
        "type": "object",
        "items": {
            "enabled": {"type": "bool", "description": "启用多Bot仲裁", "default": False},
            "emoji": {"type": "string", "description": "竞争表情", "default": "👍"},
            "window_sec": {"type": "float", "description": "竞争窗口(秒)", "default": 1.5},
        },
        "desc": "多Bot表情仲裁",
        "default": {},
        "hint": "群内多解析机器人时开启, 解析前发送竞争表情, 检测到其他bot回应则放弃",
    },
{
        "path": "cookie_health",
        "type": "object",
        "items": {
            "enabled": {"type": "bool", "description": "启用Cookie健康检查", "default": False},
            "interval_sec": {"type": "int", "description": "检查间隔(秒)", "default": 3600},
        },
        "desc": "Cookie健康检查",
        "default": {},
        "hint": "定期验证B站/知乎cookie, 失效时通知",
    }
]
"""AstrBot 专属字段声明: path=JSON路径, source=动态选项生成器(可选), default/hint/desc=静态元数据"""


def _bridge_cfg(key: str, default=None):
    """读取 bridge 语义配置 (非硬编码: 缺失回退默认值)."""
    from bridge.cfg import read_cfg
    return read_cfg(BridgeConfig._source, key, default)

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
    """0-hardcode 动态注入: 扫描上游 Config 模型 → 填充 _conf_schema.json

    无硬编码保证:
    - 无文件时从空 dict 启动 (运行时生成, 不入库)
    - 输出顺序按 _BRIDGE_FIELDS 使用频率重排 (高频在前)
    """
    import typing

    from nonebot_plugin_parser_lite.constants import PlatformEnum
    schema_path = _CONF_SCHEMA_PATH
    flag_path = Path(__file__).parent / ".injected"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text("utf-8"))
    else:
        schema = {}
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

    # F9: platforms 模板 — 每平台独立配置 (enable/use_proxy/cookies), 动态从 BaseParser 扫描
    pfm = schema.setdefault("platforms", {"type": "template_list", "description": "平台配置", "templates": {}})
    _pf_items = {
        "enable": {"type": "bool", "description": "启用该平台解析", "default": True},
        "use_proxy": {"type": "bool", "description": "该平台走代理", "default": False},
        "cookies": {"type": "string", "description": "该平台Cookie", "default": ""},
    }
    _pf_templates = pfm.setdefault("templates", {})
    _changed_platform = False
    for _cls in BaseParser.get_all_subclass():
        _p = getattr(_cls, "platform", None)
        _pname = getattr(_p, "name", None)
        if not _pname:
            continue
        _pname = str(_pname).lower()
        if _pname not in _pf_templates:
            _pf_templates[_pname] = {
                "name": getattr(_p, "display_name", _pname),
                "items": {k: dict(v) for k, v in _pf_items.items()},
            }
            _changed_platform = True
    if _changed_platform:
        updated = True; injected.append("platforms")

    # 1) features: bool 字段
    bool_fields = sorted(k for k, f in _UpConfig.model_fields.items()
                         if f.annotation is bool and k.startswith("plite_"))
    _features = schema.setdefault("features", {"type": "list", "options": [], "default": []})
    if _features.get("options") == ["__INJECT__"] or not _features.get("options"):
        _features["options"] = [_label(k) for k in bool_fields]
        _features["default"] = [
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
            # AstrBot _parse_schema: object 类型必须含 items (否则 KeyError: 'items')
            if "items" in bf:
                entry["items"] = bf["items"]
            elif bf["type"] == "object":
                entry["items"] = {}
            if "items_type" in bf:
                entry["items"] = {"type": bf["items_type"]}
            # template_list 必须透传 templates (可增删列表模板)
            if "templates" in bf:
                entry["templates"] = bf["templates"]
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
        # 顺序重排: bridge 字段按 _BRIDGE_FIELDS 使用频率序 (高频在前), 其余保持
        _ordered = {}
        _bridge_paths = [bf["path"] for bf in _BRIDGE_FIELDS]
        for _key in _bridge_paths:
            if _key in schema:
                _ordered[_key] = schema[_key]
        for _k, _v in schema.items():
            if _k not in _ordered:
                _ordered[_k] = _v
        schema = _ordered
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
# ── 格式化 (已移至 bridge.format) ──
# ── 懒下载管理器 ──────────────────────────────────────────────────────────────
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
        self._limiter = None  # 延迟到 initialize 创建 (需要 base_dir)
        self._debouncer = None  # 链接级防抖 (E5)
        self._delay_sender = None  # 延迟发送 (F7)

    async def initialize(self) -> None:
        try:
            # 上游 render 兼容补丁: safe_src 默认 method (模板省略调用)
            try:
                from bridge.render_patch import apply_render_patch
                apply_render_patch()
            except Exception:
                pass
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
            # 频率限制器 + 防抖器 (配置驱动, data/ 下持久化)
            try:
                from bridge.debounce import make_debouncer
                from bridge.rate_limit import make_limiter
                _base_dir = os.environ.get("PARSER_LITE_BASE_DIR", str(Path.cwd() / "data"))
                self._limiter = make_limiter(Path(_base_dir) / "parser_lite")
                self._debouncer = make_debouncer(Path(_base_dir) / "parser_lite")
            except Exception:
                self._limiter = None
                self._debouncer = None
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._chromium_task = asyncio.create_task(self._auto_ensure_chromium())
            # F1: B站 UP 订阅推送 (配置驱动, 默认关闭; push 为可增删 template_list)
            self._pusher = None
            try:
                from bridge.push import make_pusher
                _push_raw = _bridge_cfg("push", []) or []
                _base_dir = os.environ.get("PARSER_LITE_BASE_DIR", str(Path.cwd() / "data"))
                self._pusher = make_pusher(Path(_base_dir) / "parser_lite")
                # template_list: [{uid, groups("1,2"), enabled}]
                _subs: dict[str, list[str]] = {}
                if isinstance(_push_raw, list):
                    for entry in _push_raw:
                        if not isinstance(entry, dict):
                            continue
                        if not entry.get("enabled", True):
                            continue
                        _uid = str(entry.get("uid", "") or "").strip()
                        _grp = str(entry.get("groups", "") or "").strip()
                        if _uid:
                            _subs[_uid] = [g.strip() for g in _grp.split(",") if g.strip()]
                elif isinstance(_push_raw, dict):
                    # 兼容旧格式 {uid: [groups]}
                    _subs = {str(k): [str(g) for g in v] for k, v in _push_raw.items()}
                self._pusher.set_subscriptions(_subs)
                _interval = float(_bridge_cfg("push_interval", 300) or 300)

                async def _push_send(msg: str, groups: list[str]):
                    for gid in groups:
                        try:
                            await self.context.send_message(
                                f"aiocqhttp:GroupMessage:{gid}", [Comp.Plain(msg)])
                        except Exception as _e:
                            astrbot_logger.warning(f"[ParserLite] 推送失败 {gid}: {_e}")

                if _subs:
                    self._pusher.start(_interval, _push_send)
                    astrbot_logger.info(f"[ParserLite] UP 推送已启动: {len(_subs)} 订阅, 间隔 {_interval}s")
            except Exception as _e:
                astrbot_logger.warning(f"[ParserLite] 推送初始化跳过: {_e}")
                self._pusher = None
            # F4: Cookie 健康检查 (配置驱动, 默认关闭)
            self._cookie_health = None
            try:
                from bridge.cookie_health import make_cookie_health
                _ck_cfg = _bridge_cfg("cookie_health", {}) or {}
                _base_dir = os.environ.get("PARSER_LITE_BASE_DIR", str(Path.cwd() / "data"))
                self._cookie_health = make_cookie_health(Path(_base_dir) / "parser_lite")
                _ck_interval = float(_ck_cfg.get("interval_sec", 3600) or 3600)
                _cookies = {
                    "bilibili": _bridge_cfg("plite_bili_ck", "") or "",
                    "zhihu": _bridge_cfg("plite_zhihu_ck", "") or "",
                }

                async def _ck_notify(msg: str):
                    astrbot_logger.warning(msg)
                    try:
                        await self.context.send_message(
                            "aiocqhttp:GroupMessage:0", [Comp.Plain(msg)])
                    except Exception:
                        pass

                if _ck_cfg.get("enabled", False):
                    self._cookie_health.start(_ck_interval, _cookies, _ck_notify)
                    astrbot_logger.info(f"[ParserLite] Cookie 健康检查已启动: {_ck_interval}s")
            except Exception as _e:
                astrbot_logger.warning(f"[ParserLite] Cookie 检查初始化跳过: {_e}")
                self._cookie_health = None
            # F7: 延迟发送器 (表情触发, 配置驱动)
            self._delay_sender = None
            try:
                from bridge.delay_send import make_delay_sender
                self._delay_sender = make_delay_sender()
            except Exception:
                self._delay_sender = None
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
        # F1: 停止 UP 推送轮询
        if self._pusher is not None:
            try: await self._pusher.stop()
            except Exception: pass
        # F4: 停止 cookie 健康检查
        if self._cookie_health is not None:
            try: await self._cookie_health.stop()
            except Exception: pass
        # 新版 standalone 运行时: 关闭 scheduler + BrowserManager + DOWNLOADER
        try:
            from nonebot_plugin_parser_lite.pipeline import shutdown_runtime
            await shutdown_runtime()
        except Exception:
            pass
        if self._log_bridge:
            try:
                logging.getLogger("nonebot_plugin_parser_lite").removeHandler(self._log_bridge)
            except Exception: pass

    async def _cleanup_loop(self) -> None:
        while True:
            _interval = float(_bridge_cfg("plite_cache_interval", CACHE_INTERVAL) or 3600)
            await asyncio.sleep(_interval)
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
            # 新版: 通过 BrowserManager 验证 (复用上游单例, 与 render 共用)
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            astrbot_logger.info("[ParserLite] Chromium 已就绪"); return
        except Exception: pass
        astrbot_logger.info("[ParserLite] Chromium 未安装, 异步安装中...")
        pb = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        installed = False
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
                    installed = True
                    break
                err = stderr.decode(errors="replace").strip()[-300:]
                astrbot_logger.warning(f"[ParserLite] Chromium 安装失败 ({name}): rc={proc.returncode} {err}")
            except asyncio.TimeoutError:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装超时 ({name})")
            except Exception as e:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装异常 ({name}): {e}")
        if not installed:
            # 浏览器二进制已下载但缺系统库 → 尝试 apt-get 自动补齐
            missing = _detect_missing_libs()
            if missing:
                astrbot_logger.warning(f"[ParserLite] 检测到缺失系统库, 尝试 apt-get 安装:\n{missing}")
                try:
                    _apt_proc = await asyncio.create_subprocess_exec(
                        "apt-get", "update",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await asyncio.wait_for(_apt_proc.communicate(), timeout=300)
                    _apt_proc = await asyncio.create_subprocess_exec(
                        "apt-get", "install", "-y", "--no-install-recommends",
                        "libnspr4", "libnss3", "libgbm1", "libasound2", "libxkbcommon0",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    _out, _err = await asyncio.wait_for(_apt_proc.communicate(), timeout=600)
                    if _apt_proc.returncode == 0:
                        astrbot_logger.info("[ParserLite] 系统库安装完成, 验证 Chromium...")
                        try:
                            from nonebot_plugin_parser_lite.utils.browser import (
                                BrowserManager,
                            )
                            await BrowserManager.ensure_started()
                            astrbot_logger.info("[ParserLite] Chromium 已就绪 (系统库补齐后)")
                            return
                        except Exception as _e2:
                            astrbot_logger.error(
                                f"[ParserLite] ✗ 系统库已安装但 Chromium 仍无法启动: {_e2}")
                    else:
                        astrbot_logger.error(
                            f"[ParserLite] ✗ apt-get 安装系统库失败: rc={_apt_proc.returncode} "
                            f"{_err.decode(errors='replace').strip()[-300:]}")
                except asyncio.TimeoutError:
                    astrbot_logger.error("[ParserLite] ✗ apt-get 安装系统库超时")
                except Exception as _e3:
                    astrbot_logger.error(f"[ParserLite] ✗ apt-get 异常: {_e3}")
            # 显式报错: 列出缺失库 + 修复指引
            _missing_now = _detect_missing_libs()
            astrbot_logger.error(
                "[ParserLite] ✗✗ Chromium 环境自动组装失败, 卡片渲染将回退为文本 ✗✗\n"
                f"缺失系统库:\n{_missing_now or '(未检测到缺失库, 请检查 playwright 安装)'}\n"
                "修复方式(需容器 root):\n"
                "  1) apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0\n"
                "  2) 或运行: python -m playwright install-deps chromium\n"
                "  3) 或发送指令 /parse_install_chromium 重试浏览器下载")
            return
        # 二进制就绪后仍验证启动 (apt 补库可能仍失败)
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            astrbot_logger.info("[ParserLite] Chromium 已就绪")
        except Exception as _e:
            _missing_after = _detect_missing_libs()
            astrbot_logger.error(
                f"[ParserLite] ✗ Chromium 已下载但无法启动: {_e}\n"
                f"缺失系统库: {_missing_after or '(none detected)'}\n"
                "请运行: apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0"
                " 或 python -m playwright install-deps chromium")

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
        return url_from_text(event.get_message_str)

    @classmethod
    def _extract_urls(cls, event: AstrMessageEvent) -> list[str]:
        import astrbot.api.message_components as _Comp
        return extract_urls(event, _Comp)

    @staticmethod
    def _reply_urls(event: AstrMessageEvent) -> list[str]:
        """从被回复消息中提取 URL — 小程序卡片链接的逃生通道.

        兼容 OneBot reply 段 (data.text / data.message) 与 AstrBot message_obj 链.
        """
        urls: list[str] = []
        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or []
        for seg in chain if isinstance(chain, list) else []:
            seg_type = str(seg.get("type", "")) if isinstance(seg, dict) else ""
            if "reply" not in seg_type:
                continue
            data = seg.get("data", {}) if isinstance(seg, dict) else {}
            if not isinstance(data, dict):
                continue
            for key in ("text", "message", "content"):
                raw = data.get(key, "")
                if isinstance(raw, list):
                    for sub in raw:
                        if isinstance(sub, dict):
                            sub_data = sub.get("data", {})
                            if isinstance(sub_data, dict):
                                collect_urls(str(sub_data.get("text", "")), urls)
                                d = sub_data.get("data", "")
                                if isinstance(d, str) and d:
                                    collect_urls(d, urls)
                                    u = extract_card_json_url(d)
                                    if u:
                                        urls.append(u)
                elif isinstance(raw, str) and raw:
                    collect_urls(raw, urls)
                    u = extract_card_json_url(raw)
                    if u:
                        urls.append(u)
        # 去重
        seen = set()
        result = []
        for u in urls:
            u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

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
                _img_mb = int(_bridge_cfg("plite_image_compress_mb", 20) or 20)
                if len(raw) > _img_mb * 1024 * 1024:
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
            # F7: 大视频延迟发送 — 先发提示, 表情回应后触发
            _dl_cfg = _bridge_cfg("delay_send", {}) or {}
            if _dl_cfg.get("enabled", False) and self._delay_sender is not None:
                _threshold = int(_dl_cfg.get("threshold_mb", 20) or 20) * 1024 * 1024
                _msg_id = getattr(getattr(event, "message_obj", None), "raw_message", None)
                _msg_id = (_msg_id or {}).get("message_id") if isinstance(_msg_id, dict) else None
                if _msg_id and sz > _threshold:
                    _dl_key = f"{_msg_id}:{p.name}"
                    self._delay_sender.arm(str(_msg_id), _dl_key,
                                           timeout_sec=float(_dl_cfg.get("timeout_sec", 300) or 300))
                    async def _do_delay_send(_key):
                        try:
                            await self._send_any(event, p, "video", source_url=source_url, duration=duration)
                        except Exception:
                            pass
                    self._delay_sender.set_trigger(_do_delay_send)
                    try:
                        await event.send(event.chain_result([Comp.Plain(
                            f"视频较大 ({sz // 1024 // 1024}MB), 回应 👍 后发送")]))
                        return
                    except Exception:
                        pass

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
            data = _CARD_CACHE.pop(cache_key)  # LRU: 命中即移尾部
            _CARD_CACHE[cache_key] = data
            await event.send(event.chain_result([Comp.Image.fromBytes(data)]))
            astrbot_logger.info(f"[ParserLite] card cache hit ({len(data)} bytes)")
            return

        from nonebot_plugin_parser_lite.render import RENDERER
        # 0-hardcode: 直接复用 standalone renderer 的 Playwright 实现
        try:
            data = await RENDERER.render_image(result)
            if len(data) < 1024 or data[:2] != b"\xff\xd8":
                raise RuntimeError(f"Invalid JPEG: {len(data)} bytes")
            # E7: LRU 淘汰最旧 (OrderedDict 语义: 超限删第一个)
            if len(_CARD_CACHE) >= _CARD_CACHE_MAX:
                _CARD_CACHE.pop(next(iter(_CARD_CACHE)), None)
            _CARD_CACHE[cache_key] = data
            await event.send(event.chain_result([Comp.Image.fromBytes(data)]))
            astrbot_logger.info(f"[ParserLite] card rendered ({len(data)} bytes)")
        except Exception:
            astrbot_logger.warning(f"[ParserLite] 卡片渲染失败, 回退文本\n{traceback.format_exc()}")
            try:
                await event.send(event.chain_result([Comp.Plain(format_full(result))]))
            except Exception:
                astrbot_logger.error(
                    f"[ParserLite] 回退文本发送也失败 (OneBot API 可能不可用)\n{traceback.format_exc()}")

    # ── 自动触发的 URL 解析 ────────────────────────────────────────────────────
    async def on_url_auto(self, event: AstrMessageEvent):
        # on_message_group/on_message_private 与 regex filter 会先后触发同一条消息,
        # 导致同一 URL 被解析两次 (dedup 使用 message_id, 两个事件 id 可能不同)
        return  # 避免重复 — 群聊/私聊由 on_message_group/on_message_private 覆盖

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message_group(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_message_private(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    async def on_message(self, event: AstrMessageEvent):
        # E6: notice 事件 (表情回应) 分流到仲裁器
        try:
            from bridge.arbiter import is_notice_event
            if is_notice_event(event):
                await self.on_notice(event)
                return
        except Exception:
            pass
        # F2: QQ 卡片 → LLM 结构化文本注入 (配置驱动, 默认开)
        try:
            from bridge.card_semantic import find_json_cards, inject_card_summary
            if _bridge_cfg("card_semantic", True):
                for _entry in find_json_cards(event)[:2]:
                    inject_card_summary(event, _entry["card"])
        except Exception:
            pass
        await self._handle_card_message(event)

    def _should_send(self, media_type: str) -> bool:
        """发送策略门: 从配置读取, 默认从 _send_any 自动扫描的全部类型"""
        try:
            s = _bridge_cfg("send_strategy", _get_sendable_types())
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
            # bridge 语义字段从 _source 读取 (不在上游 Config 模型)
            _direct = bool(_bridge_cfg("plite_direct_link", False))
            _cover_only = bool(_bridge_cfg("plite_send_cover_only", False))
            # F5: 直链免下载模式 (配置驱动, 非硬编码)
            if _direct and src_url:
                sent = await self._try_direct_send(event, item, src_url)
                if sent:
                    return
            # F6: 视频仅发封面 (配置驱动)
            if isinstance(item, VideoContent) and _cover_only:
                if self._should_send("image"):
                    await self._send_video_cover(event, item)
                return
            p = Path(str(await item.path_task))
            if isinstance(item, (ImageContent, GraphicContent, StickerContent)):
                if self._should_send("image"):
                    await self._send_any(event, p, "image", source_url=src_url)
            elif isinstance(item, VideoContent):
                if self._should_send("video"):
                    await self._send_any(event, p, "video", source_url=src_url, duration=dur)
            elif isinstance(item, AudioContent):
                if self._should_send("audio"):
                    await self._send_any(event, p, "audio", source_url=src_url, duration=dur)
        except Exception: pass

    async def _try_direct_send(self, event: AstrMessageEvent, item, src_url: str) -> bool:
        """F5: HEAD+Range 探测大小, 未超限则 URL 直发 (免下载). 失败返回 False."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.head(src_url, headers={"Range": "bytes=0-0"})
                size = None
                cr = resp.headers.get("content-range", "")
                cl = resp.headers.get("content-length", "")
                if cr and "/" in cr:
                    size = int(cr.split("/")[-1])
                elif cl and cl.isdigit():
                    size = int(cl)
            if size is None:
                return False
            max_mb = int(get_config().max_size)
            if size > max_mb * 1024 * 1024:
                return False  # 超限回退下载
            if isinstance(item, VideoContent):
                if self._should_send("video"):
                    await event.send(event.chain_result(
                        [Comp.Video.fromURL(src_url)]))
                return True
            if isinstance(item, (ImageContent, GraphicContent)):
                if self._should_send("image"):
                    await event.send(event.chain_result(
                        [Comp.Image.fromURL(src_url)]))
                return True
            return False
        except Exception:
            return False

    async def _send_video_cover(self, event: AstrMessageEvent, item) -> None:
        """F6: ffmpeg 截帧发视频封面."""
        try:
            from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
            if not await FFmpeg.is_available():
                return
            vpath = Path(str(await item.path_task))
            cover = vpath.parent / f"{vpath.stem}_cover.jpg"
            await FFmpeg.exec_ffmpeg([
                "-i", str(vpath), "-frames:v", "1", "-q:v", "5",
                "-y", str(cover),
            ])
            if cover.exists():
                if self._should_send("image"):
                    await self._send_any(event, cover, "image",
                                         source_url=getattr(item.path_task, "url", ""))
                cover.unlink(missing_ok=True)
        except Exception:
            pass

    async def _send_as_forward(self, event: AstrMessageEvent, items: list, result: ParseResult):
        """合并转发: 将多项媒体内容打包为 Comp.Nodes (移植自上游 Renderer.__build_forward_segs)"""
        nodes = []
        author = result.author.name if result.author and result.author.name else "解析"
        platform = result.platform.display_name if result.platform else ""
        MAX_PER_NODE = int(_bridge_cfg("plite_forward_max_nodes", 90) or 90)

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
            # E4: 发送降级链 — 合并转发失败 → 逐项单发 (动态降级, 无硬编码)
            from bridge.fallback import send_with_fallback

            async def _try_forward() -> bool:
                await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))
                return True

            async def _try_individual() -> bool:
                sent_any = False
                for _node in nodes:
                    for _seg in getattr(_node, "content", []) or []:
                        try:
                            await event.send(event.chain_result([_seg]))
                            sent_any = True
                        except Exception:
                            pass
                return sent_any

            await send_with_fallback(
                try_send=_try_forward,
                fallbacks=[_try_individual],
                logger=astrbot_logger,
                label="合并转发",
            )

    async def on_notice(self, event: AstrMessageEvent):
        """E6: 多 Bot 表情仲裁 + F7: 延迟发送触发 — 处理 group_msg_emoji_like notice.

        AstrBot 将 OneBot notice 事件转为 AstrMessageEvent (raw_message 保留原始 dict).
        """
        try:
            from bridge.arbiter import check_notice, parse_notice
            raw = getattr(event, "raw_message", None)
            if isinstance(raw, dict):
                parsed = parse_notice(raw)
                if parsed:
                    msg_id, emoji_id = parsed
                    # F7: 延迟发送触发 (先于仲裁, 互不冲突)
                    if self._delay_sender is not None:
                        _dl_cfg = _bridge_cfg("delay_send", {}) or {}
                        _want = [str(x) for x in (_dl_cfg.get("emoji_ids", []) or [])]
                        if self._delay_sender.on_emoji_like(msg_id, emoji_id, _want):
                            astrbot_logger.info(f"[ParserLite] 延迟发送触发: msg={msg_id}")
                            return
                    # E6: 仲裁
                    if check_notice(msg_id, emoji_id):
                        astrbot_logger.debug(f"[ParserLite] 仲裁: 其他 bot 已竞争 {msg_id}, 放弃")
        except Exception:
            pass

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
        _dedup_ttl = float(_bridge_cfg("plite_dedup_ttl", 60) or 60)
        if msg_id in self._recently_processed:
            if now - self._recently_processed[msg_id] < _dedup_ttl:
                return
        self._recently_processed[msg_id] = now
        if len(self._recently_processed) > 50:
            cutoff = now - _dedup_ttl
            self._recently_processed = {k: v for k, v in self._recently_processed.items() if v > cutoff}
        # E6: 多 Bot 仲裁 — 武装竞争窗口 (参数动态, 默认关闭)
        _arbiter_cfg = _bridge_cfg("arbiter", {}) or {}
        if _arbiter_cfg.get("enabled", False):
            try:
                from bridge.arbiter import arm
                _emoji = _arbiter_cfg.get("emoji", "") or None
                _win = _arbiter_cfg.get("window_sec", None)
                if not arm(str(msg_id), emoji=_emoji, window_sec=_win):
                    astrbot_logger.debug("[ParserLite] 仲裁: 已放弃此消息")
                    return
            except Exception:
                pass
        urls = self._extract_urls(event)
        if not urls:
            urls = self._reply_urls(event)  # 引用消息逃生通道 (小程序卡片)
        if not urls: return
        if self._disabled(event) or self._blacklisted(event): return
        # 频率限制 (配置驱动)
        if self._limiter is not None:
            from bridge.rate_limit import clean_url, load_rate_cfg
            _rcfg = load_rate_cfg(BridgeConfig._source)
            _sender = event.get_sender_id() or ""
            for _u in urls[:3]:
                _ok, _why = self._limiter.allow(url=clean_url(_u), user_id=str(_sender), cfg=_rcfg)
                if not _ok:
                    astrbot_logger.info(f"[ParserLite] 限频: {_why}")
                    try:
                        await event.send(event.chain_result([Comp.Plain(_why)]))
                    except Exception:
                        pass
                    return
        for url in urls[:3]:
            # E5: 链接级防抖 (持久化) — 窗口秒数动态从配置读取, 失败回滚
            if self._debouncer is not None:
                from bridge.debounce import debounce_key
                from bridge.rate_limit import clean_url
                _session = self._key(event)
                _dkey = debounce_key(_session, clean_url(url))
                _dwin = float(getattr(get_config(), "lazy_download_timeout", 300) or 300)
                if not self._debouncer.should_parse(_dkey, _dwin):
                    continue  # 防抖命中
            try:
                result = await self._parse_raw(url)
                if result is None:
                    if self._debouncer is not None:
                        self._debouncer.rollback(_dkey)  # 失败回滚, 允许重试
                    continue
                if self._debouncer is not None:
                    self._debouncer.mark_success(_dkey)
                if self._should_send("card"):
                    await self._send_card(event, result)
                await self._send_items(event, result.content, result)
            except Exception:
                if self._debouncer is not None:
                    self._debouncer.rollback(_dkey)
                astrbot_logger.error(f"[ParserLite] _handle_card_message 异常\n{traceback.format_exc()}")

    # ── 命令 ──────────────────────────────────────────────────────────────────
    async def cmd_parse(self, event: AstrMessageEvent):
        if self._blacklisted(event) or self._disabled(event):
            yield event.plain_result("本群已禁用"); return
        urls = self._extract_urls(event)
        if not urls:
            urls = self._reply_urls(event)  # 引用消息逃生通道 (小程序卡片)
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
        """自检: 全动态扫描, 结构化可观测, 错误显式返回 (复用 bridge.doctor)."""
        try:
            from bridge.doctor import render_text, run_checks, save_snapshot, summarize
            results = await run_checks()
            summary = summarize(results)
            report = render_text(results, summary)
            # 错误显式返回: 有失败项时附修复提示 + 快照落盘
            if summary["failed"] or summary["warn"]:
                snap = save_snapshot(results, summary)
                report += "\n\n── 修复建议 ──"
                report += "\n  1. Config/Downloader 失败 → 检查插件配置与依赖"
                report += "\n  2. Chromium 警告 → 发送 parse_install_chromium"
                report += "\n  3. Network 失败 → 检查代理/网络"
                report += "\n  4. 其余失败 → 查看上方 error 详情"
                if snap:
                    report += f"\n\n快照已保存: {snap}"
            yield event.plain_result(report)
        except Exception as e:
            yield event.plain_result(f"doctor 执行失败: {e}")

    async def cmd_install_chromium(self, event: AstrMessageEvent):
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            yield event.plain_result("Chromium 已可用, 无需重复安装"); return
        except Exception: pass
        yield event.plain_result("开始安装 Chromium (耗时较长, 请等待)...")
        pb = str(Path(get_config().data_dir) / "playwright_browsers")
        installed = False
        for url, name in [("https://npmmirror.com/mirrors/playwright","npmmirror"),
                          ("https://playwright.azureedge.net","Azure")]:
            env = os.environ.copy(); env["PLAYWRIGHT_BROWSERS_PATH"] = pb
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
            try:
                yield event.plain_result(f"尝试 {name} ({url}) ...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium", env=env)
                await asyncio.wait_for(proc.wait(), timeout=600)
                if proc.returncode != 0:
                    yield event.plain_result(f"{name} 安装失败 (rc={proc.returncode}), 切换镜像...")
                    continue
                installed = True
                break
            except asyncio.TimeoutError:
                yield event.plain_result(f"{name} 超时, 切换镜像...")
            except Exception as e:
                yield event.plain_result(f"{name} 失败: {e}\n切换镜像...")
        if not installed:
            yield event.plain_result("✗ 浏览器下载失败, 请检查网络或手动执行: python -m playwright install chromium")
            return
        # 浏览器就绪 → 检查/补齐系统库 (install-deps 优先, apt-get 回退)
        missing = _detect_missing_libs()
        if missing:
            yield event.plain_result(f"检测到缺失系统库, 尝试自动安装:\n{missing}")
            if not (hasattr(os, "geteuid") and os.geteuid() == 0):
                yield event.plain_result(
                    "✗ 非 root 用户无法安装系统库, 请在容器/服务器以 root 运行:\n"
                    "  apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0\n"
                    "  或: python -m playwright install-deps chromium")
                return
            # ① playwright install-deps (全量依赖, 适配发行版包管理器)
            try:
                _deps_proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install-deps", "chromium",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _deps_out, _deps_err = await asyncio.wait_for(
                    _deps_proc.communicate(), timeout=600)
                if _deps_proc.returncode == 0:
                    astrbot_logger.info("[ParserLite] playwright install-deps 成功")
                else:
                    yield event.plain_result(
                        f"playwright install-deps 失败 (rc={_deps_proc.returncode}), "
                        f"回退 apt-get:\n{_deps_err.decode(errors='replace').strip()[-200:]}")
                    # ② 回退: 手写 apt-get 补齐核心库
                    try:
                        _apt1 = await asyncio.create_subprocess_exec(
                            "apt-get", "update",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await asyncio.wait_for(_apt1.communicate(), timeout=300)
                        _apt2 = await asyncio.create_subprocess_exec(
                            "apt-get", "install", "-y", "--no-install-recommends",
                            "libnspr4", "libnss3", "libgbm1", "libasound2", "libxkbcommon0",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        _o2, _e2 = await asyncio.wait_for(_apt2.communicate(), timeout=600)
                        if _apt2.returncode != 0:
                            yield event.plain_result(
                                f"✗ apt-get 安装失败: rc={_apt2.returncode} "
                                f"{_e2.decode(errors='replace').strip()[-300:]}")
                            yield event.plain_result(
                                "请手动执行: apt-get update && apt-get install -y "
                                "libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0")
                            return
                    except Exception as e:
                        yield event.plain_result(f"✗ apt-get 异常: {e}\n请手动安装系统库后重试")
                        return
            except asyncio.TimeoutError:
                yield event.plain_result("✗ playwright install-deps 超时, 请手动安装系统库后重试")
                return
            except Exception as e:
                yield event.plain_result(f"✗ 系统库安装异常: {e}\n请手动安装后重试")
                return
        # 最终验证
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            yield event.plain_result("✓ Chromium 安装完成且可启动!")
        except Exception as e:
            yield event.plain_result(
                f"✗ Chromium 仍无法启动: {e}\n缺失库: {_detect_missing_libs() or '(none)'}\n"
                "请运行: python -m playwright install-deps chromium")

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
filter.command("parser_doctor")(ParserLitePlugin.cmd_doctor)  # 别名 (用户习惯 /parser_doctor)
filter.command("parse_install_chromium")(ParserLitePlugin.cmd_install_chromium)
filter.command("cmd_bm")(ParserLitePlugin.cmd_bm)
filter.command("cmd_blogin")(ParserLitePlugin.cmd_blogin)
filter.regex(r"^(xz|下载)$")(ParserLitePlugin._on_download_trigger)
filter.regex(r"https?://")(ParserLitePlugin.on_url_auto)
filter.llm_tool(name="parse_url")(ParserLitePlugin.parse_url)
