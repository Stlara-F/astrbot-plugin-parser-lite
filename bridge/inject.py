"""0 硬编码动态注入决策树 (配置骨架 → _conf_schema.json).

设计 (参考 PR #1):
- _conf_schema.json 仅提交 features: ["__INJECT__"] 骨架 (或运行时生成)
- 模块加载时动态扫描: 上游 Config 模型字段 / BaseParser 平台 / 枚举 / CustomParser.SCHEMA
- .injected 标记文件防止重复注入, 不覆盖用户 WebUI 编辑
"""

from __future__ import annotations

import json
from pathlib import Path
import typing

from bridge.context import label, up_base_parser, up_config
from bridge.core import CustomParser


def schema_desc(fname: str) -> str:
    s = fname.removeprefix("plite_").replace("_", " ")
    return " ".join(w[0].upper() + w[1:] for w in s.split())


def is_enum_field(finfo) -> bool:
    ann = finfo.annotation
    if hasattr(ann, "__args__"):
        ann = typing.get_args(ann)[0]
    return hasattr(ann, "__members__")


def is_str_list_field(finfo) -> bool:
    ann = finfo.annotation
    if not hasattr(ann, "__args__"):
        return False
    args = typing.get_args(ann)
    return len(args) == 1 and args[0] is str


def extract_slider(finfo) -> dict | None:
    """从 pydantic Field 元数据提取 slider 配置."""
    slider = None
    for meta in getattr(finfo, "metadata", []):
        ge_ = getattr(meta, "ge", None)
        le_ = getattr(meta, "le", None)
        multiple = getattr(meta, "multiple_of", None)
        if ge_ is not None or le_ is not None:
            slider = {}
            if ge_ is not None:
                slider["min"] = ge_
            if le_ is not None:
                slider["max"] = le_
            slider["step"] = multiple if multiple else (
                max(1, (slider["max"] - slider.get("min", 0)) // 20)
                if "min" in slider and "max" in slider else 1
            )
            break
    return slider


def _build_field_entry(fname: str, finfo, slider_hints: dict) -> dict | None:
    """从 pydantic 字段信息生成 AstrBot schema 条目 (0 hardcode)."""
    ann = finfo.annotation
    default = finfo.default
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

    entry = {"description": schema_desc(fname)}
    if ann is int or (hasattr(ann, "__origin__") and ann.__origin__ is int):
        entry["type"] = "int"
        slider = extract_slider(finfo) or slider_hints.get(fname)
        if slider:
            entry["slider"] = slider
    elif ann is str or (hasattr(ann, "__origin__") and ann.__origin__ is str):
        entry["type"] = "string"
    elif ann is bool or (hasattr(ann, "__origin__") and ann.__origin__ is bool):
        return None  # bool → features
    elif is_str_list_field(finfo):
        entry["type"] = "list"
        entry["items"] = {"type": "string"}
    elif is_enum_field(finfo):
        return None  # enum → parser_extra
    elif ann is float or (hasattr(ann, "__origin__") and ann.__origin__ is float):
        entry["type"] = "float"
    else:
        return None
    if default_val is not None:
        entry["default"] = default_val
    return entry


def _get_sendable_types() -> list[str]:
    """动态扫描上游 ContentItem Union 成员 → 可发送类型列表 (0 hardcode)."""
    from nonebot_plugin_parser_lite.data import ContentItem

    _by = {}
    for cls in getattr(ContentItem, "__args__", []) or []:
        if hasattr(cls, "__name__"):
            _by[cls.__name__] = cls
    types = ["card"]
    for name, cls in _by.items():
        if name in ("ImageContent", "VideoContent", "AudioContent"):
            types.append({"ImageContent": "image", "VideoContent": "video",
                          "AudioContent": "audio"}[name])
    return list(dict.fromkeys(types))


# ── 桥接扩展字段声明 (非硬编码平台: 仅桥接功能开关) ──────────────────────────
_BRIDGE_FIELDS: list[dict] = [
    {"path": "plite_http_proxy", "type": "string", "desc": "HTTP代理", "default": "",
     "hint": "全局HTTP/HTTPS代理地址。例: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080。留空则不使用代理。平台级走代理需在 platforms 勾选 proxy"},
    {"path": "send_strategy", "type": "list", "desc": "发送策略",
     "default": _get_sendable_types, "options": _get_sendable_types},
    {"path": "plite_direct_link", "type": "bool", "desc": "直链免下载模式", "default": False,
     "hint": "开启后视频/图片优先以 URL 直链发送, 不落盘"},
    {"path": "plite_send_cover_only", "type": "bool", "desc": "视频仅发封面", "default": False},
    {"path": "plite_image_compress_mb", "type": "int", "desc": "图片压缩阈值MB", "default": 20},
    {"path": "plite_dedup_ttl", "type": "int", "desc": "链接去重TTL秒", "default": 60},
    {"path": "plite_cache_interval", "type": "int", "desc": "缓存清理间隔秒", "default": 3600},
    {"path": "plite_forward_max_nodes", "type": "int", "desc": "合并转发最大节点数", "default": 50},
    {"path": "card_semantic", "type": "bool", "desc": "QQ卡片语义注入", "default": True},
    {"path": "push", "type": "template_list", "desc": "B站UP订阅推送", "default": [],
     "templates": {"default": {"name": "订阅", "items": {
         "uid": {"type": "string", "description": "UP的UID", "default": ""},
         "groups": {"type": "string", "description": "群号(逗号分隔)", "default": ""},
         "enabled": {"type": "bool", "description": "启用", "default": True},
     }}}},
    {"path": "push_interval", "type": "int", "desc": "推送轮询间隔秒", "default": 300},
    {"path": "delay_send", "type": "object", "desc": "延迟发送表情触发", "default": {},
     "items": {
         "enabled": {"type": "bool", "description": "启用", "default": False},
         "threshold_mb": {"type": "int", "description": "阈值MB", "default": 20},
         "timeout_sec": {"type": "int", "description": "超时秒", "default": 300},
         "emoji_ids": {"type": "list", "description": "触发表情ID", "items": {"type": "string"}, "default": ["128077"]},
     }},
    {"path": "arbiter", "type": "object", "desc": "多Bot表情仲裁", "default": {},
     "items": {
         "enabled": {"type": "bool", "description": "启用", "default": False},
         "emoji": {"type": "string", "description": "竞争表情", "default": "👍"},
         "window_sec": {"type": "float", "description": "窗口秒", "default": 1.5},
     }},
    {"path": "cookie_health", "type": "object", "desc": "Cookie健康检查", "default": {},
     "items": {
         "enabled": {"type": "bool", "description": "启用", "default": False},
         "interval_sec": {"type": "int", "description": "间隔秒", "default": 3600},
     }},
]

_PARSER_EXTRA_MAP: dict[str, tuple[str, type, bool]] = {}


def get_parser_extra_mapping() -> dict:
    if not _PARSER_EXTRA_MAP:
        _rebuild_parser_extra_map()
    return dict(_PARSER_EXTRA_MAP)


def _rebuild_parser_extra_map() -> None:
    """注入跳过时仍重建映射表 (保证运行时值同步可用)."""
    _UpConfig = up_config()
    _PARSER_EXTRA_MAP.clear()
    for fname, finfo in _UpConfig.model_fields.items():
        if not is_enum_field(finfo):
            continue
        ann = finfo.annotation
        is_list = hasattr(ann, "__args__")
        enum_cls = typing.get_args(ann)[0] if is_list else ann
        short_key = fname.removeprefix("plite_")
        _PARSER_EXTRA_MAP[short_key] = (fname, enum_cls, is_list)


def inject_dynamic_options_static(schema_path: Path, flag_path: Path) -> list[str]:
    """0-hardcode 动态注入: 扫描上游 Config 模型 → 填充 _conf_schema.json.

    :return: 注入的配置项名列表
    """
    import logging

    _logger = logging.getLogger("nonebot_plugin_parser_lite")
    from nonebot_plugin_parser_lite.constants import PlatformEnum

    _UpConfig = up_config()
    BaseParser = up_base_parser()

    schema = json.loads(schema_path.read_text("utf-8")) if schema_path.exists() else {}
    has_markers = "__INJECT__" in json.dumps(schema)
    if flag_path.exists() and not has_markers:
        _rebuild_parser_extra_map()
        return []
    updated = False
    injected: list[str] = []

    # 0) custom_parsers 模板: 从 CustomParser.SCHEMA 扫描生成
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
        updated = True
        injected.append("custom_parsers")

    # platforms 模板: 每平台独立配置 (enable/proxy/cookies), 动态从 BaseParser 扫描
    pfm = schema.setdefault("platforms", {"type": "template_list", "description": "平台配置", "templates": {}})
    _pf_items = {
        "enable": {"type": "bool", "description": "启用该平台解析", "default": True},
        "proxy": {"type": "bool", "description": "该平台走代理", "default": False},
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
        updated = True
        injected.append("platforms")

    # 1) features: bool 字段
    bool_fields = sorted(k for k, f in _UpConfig.model_fields.items()
                         if f.annotation is bool and k.startswith("plite_"))
    _features = schema.setdefault("features", {"type": "list", "options": [], "default": []})
    if _features.get("options") == ["__INJECT__"] or not _features.get("options"):
        _features["options"] = [label(k) for k in bool_fields]
        _features["default"] = [
            label(k) for k in bool_fields if _UpConfig.model_fields[k].default is True
        ]
        updated = True
        injected.append("features")

    # 2) plite_* 顶级字段: 从上游模型自动生成
    _slider_hints = {
        "plite_max_size": {"min": 10, "max": 500, "step": 10},
        "plite_duration_maximum": {"min": 30, "max": 3600, "step": 30},
    }
    for fname, finfo in _UpConfig.model_fields.items():
        if not fname.startswith("plite_"):
            continue
        ann = finfo.annotation
        is_bool = ann is bool
        is_enum = is_enum_field(finfo)
        if is_bool or is_enum:
            continue  # bool → features; enum → parser_extra
        if fname in schema and schema[fname] != ["__INJECT__"] and not isinstance(schema[fname], list):
            default = finfo.default
            if default is not None and not isinstance(default, (int, float, str, bool, list, dict)):
                default = None
            if default is not None and "default" in schema[fname]:
                schema[fname]["default"] = default
                updated = True
            continue
        entry = _build_field_entry(fname, finfo, _slider_hints)
        if entry:
            schema[fname] = entry
            updated = True
            injected.append(fname)

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
            if "items" in bf:
                entry["items"] = bf["items"]
            elif bf["type"] == "object":
                entry["items"] = {}
            if "items_type" in bf:
                entry["items"] = {"type": bf["items_type"]}
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
            updated = True
            injected.append(bf["path"])

    # 4) plite_disabled_platforms
    platforms = sorted({p.name for p in PlatformEnum})
    if schema.get("plite_disabled_platforms", {}).get("options") in (["__INJECT__"], None):
        schema["plite_disabled_platforms"] = {
            "type": "list",
            "description": schema_desc("plite_disabled_platforms"),
            "options": platforms,
            "default": [],
        }
        updated = True
        injected.append("plite_disabled_platforms")

    # 5) parser_extra: 枚举字段
    _PARSER_EXTRA_MAP.clear()
    extra = {}
    for fname, finfo in _UpConfig.model_fields.items():
        if not is_enum_field(finfo):
            continue
        if fname in schema:
            continue
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
        if (not schema.get("parser_extra", {}).get("items")
                or short_key not in schema["parser_extra"].get("items", {})):
            extra[short_key] = {
                "description": schema_desc(fname),
                "type": "string" if not is_list else "list",
                "options": list(enum_cls.__members__),
                "default": fallback,
                "hint": "",
            }
    if extra:
        schema.setdefault("parser_extra", {"type": "object", "description": "解析器专属扩展", "items": {}})
        schema["parser_extra"]["items"] = {**(schema.get("parser_extra", {}).get("items") or {}), **extra}
        updated = True
        injected.append("parser_extra")

    # 6) test_urls
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
        updated = True
        injected.append("test_urls")

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
        _logger.info(f"[ParserLite] schema injected: {', '.join(injected) if injected else '(defaults sync)'}")
    return injected
