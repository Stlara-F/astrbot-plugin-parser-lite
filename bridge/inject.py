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

from bridge.context import up_base_parser, up_config
from bridge.i18n import (  # noqa: F401 (翻译/标签单一来源, 兼容外部引用)
    TRANSLATIONS,
    is_bool_annotation,
    label,
    tr,
)
from bridge.send import get_sendable_types  # noqa: F401 (发送类型单一来源)


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
            slider["step"] = (
                multiple
                if multiple
                else (
                    max(1, (slider["max"] - slider.get("min", 0)) // 20)
                    if "min" in slider and "max" in slider
                    else 1
                )
            )
            break
    return slider


def is_bool_field(finfo) -> bool:
    """bool 字段判定 — 委托 i18n.is_bool_annotation (单一来源, 顶部已 import)."""
    return is_bool_annotation(finfo.annotation)


def _build_field_entry(fname: str, finfo, slider_hints: dict) -> dict | None:
    """从 pydantic 字段信息生成 AstrBot schema 条目 (0 hardcode)."""
    ann = finfo.annotation
    default = finfo.default
    try:
        if default is not None and not isinstance(
            default, (int, float, str, bool, list, dict)
        ):
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

    entry = {"description": tr(fname)}
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


# ── 桥接扩展字段声明 (仅 AstrBot 发送适配字段; r8: 自研业务模块字段已删) ─────
_BRIDGE_FIELDS: list[dict] = [
    {
        "path": "send_strategy",
        "type": "list",
        "desc": tr("send_strategy"),
        "default": get_sendable_types,
        "options": get_sendable_types,
    },
    {
        "path": "plite_direct_link",
        "type": "bool",
        "desc": tr("plite_direct_link"),
        "default": False,
        "hint": "开启后视频/图片优先以 URL 直链发送, 不落盘",
    },
    {
        "path": "plite_send_cover_only",
        "type": "bool",
        "desc": tr("plite_send_cover_only"),
        "default": False,
    },
    {
        "path": "plite_video_file_threshold_mb",
        "type": "int",
        "desc": tr("plite_video_file_threshold_mb"),
        "default": 100,
        "hint": "视频超过此大小(MB)时以文件形式发送 (OneBot11 base64 有上限, 默认 100MB; 20MB 以上 base64 亦转文件)",
    },
    {
        "path": "plite_forward_max_nodes",
        "type": "int",
        "desc": tr("plite_forward_max_nodes"),
        "default": 50,
    },
]

_PARSER_EXTRA_MAP: dict[str, tuple[str, type, bool]] = {}

_BRIDGE_PATHS: list[str] = [bf["path"] for bf in _BRIDGE_FIELDS]

# 已删自研字段清理列表 (r7/r8/r9 删除模块对应键, 注入时从用户配置清除回潮残留)
_STALE_CONFIG_KEYS = (
    "parsers",
    "custom_parsers",
    "test_urls",
    "plite_disabled_platforms",
    "plite_image_compress_mb",
    "plite_http_proxy",
    "plite_md5_fast_send",
    "plite_md5_cache_max",
    "plite_dedup_ttl",
    "plite_cache_interval",
    "card_semantic",
    "push",
    "push_interval",
    "delay_send",
    "arbiter",
    "cookie_health",
)

# 配置 schema 版本: 新增配置字段时递增 → 触发重新注入 (保留用户已编辑字段值)
SCHEMA_VERSION = 7

# 注入反馈: 成功/失败报告 (模块加载与 WebUI 诊断可查询)
inject_report: dict = {
    "last_ok": None,  # 最近一次注入是否成功 (True/False)
    "last_error": "",  # 失败原因 (含 Traceback 摘要)
    "injected": [],  # 最近一次注入的配置项
    "schema_version": SCHEMA_VERSION,
}


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
    :note: 注入反馈 (成功/失败) 写入 inject_report, 异常不阻断插件加载
    """
    import logging
    import traceback

    _logger = logging.getLogger("nonebot_plugin_parser_lite")
    try:
        _result = _inject_inner(schema_path, flag_path, _logger)
        inject_report.update(
            {
                "last_ok": True,
                "last_error": "",
                "injected": _result,
                "schema_version": SCHEMA_VERSION,
            }
        )
        _logger.info(
            f"[ParserLite] schema injected OK: {', '.join(_result) if _result else '(idempotent sync)'}"
        )
        return _result
    except Exception as _e:
        # 注入失败反馈: 记录完整 Traceback, 不阻断插件加载 (schema 缺失时 AstrBot 用内置默认)
        _tb = traceback.format_exc(limit=8)
        inject_report.update(
            {
                "last_ok": False,
                "last_error": f"{type(_e).__name__}: {_e}",
                "injected": [],
                "schema_version": SCHEMA_VERSION,
            }
        )
        _logger.error(f"[ParserLite] schema 注入失败 (插件仍可加载): {_e}\n{_tb}")
        return []


def _inject_inner(schema_path: Path, flag_path: Path, _logger) -> list[str]:
    """注入主体 (被 inject_dynamic_options_static 包裹以提供失败反馈)."""

    _UpConfig = up_config()
    BaseParser = up_base_parser()

    schema = json.loads(schema_path.read_text("utf-8")) if schema_path.exists() else {}
    has_markers = "__INJECT__" in json.dumps(schema)
    # 版本化标记: 同版本仅幂等同步 (描述/结构/options, 保留用户编辑);
    # 版本变化 (插件更新新增字段) → 完整注入 (新增字段 + 默认值对齐)
    flag_version = flag_path.read_text("utf-8").strip() if flag_path.exists() else ""
    _inject_new = (flag_version != str(SCHEMA_VERSION)) or has_markers
    updated = False
    injected: list[str] = []

    # r8: custom_parsers 注入已删 (自研正则自定义解析器模块移除);
    # 旧 schema 残留键清理 (防 AstrBot 陈旧残留显示)
    if "custom_parsers" in schema:
        schema.pop("custom_parsers", None)
        updated = True

    # platforms: 统一勾选列表 (27 平台模板废弃) — enabled 全局勾选,
    # cookies 为动态模板列表 (平台下拉仅含源码支持平台)
    # 旧格式 (template_list 27 模板) → 强制重建为 object 勾选结构
    _old_pfm = schema.get("platforms")
    if isinstance(_old_pfm, list) or (
        isinstance(_old_pfm, dict) and _old_pfm.get("type") != "object"
    ):
        schema["platforms"] = {
            "type": "object",
            "description": tr("platforms"),
            "items": {},
        }
        updated = True
    pfm = schema.setdefault(
        "platforms", {"type": "object", "description": tr("platforms"), "items": {}}
    )
    pfm.setdefault("type", "object")
    pfm.setdefault("description", tr("platforms"))
    pfm.setdefault("items", {})
    _plats = []
    for _cls in BaseParser.get_all_subclass():
        _p = getattr(_cls, "platform", None)
        _pname = getattr(_p, "name", None)
        if _pname:
            _plats.append(str(_pname).lower())
    _plats = sorted(set(_plats))
    _pf_items = pfm.setdefault("items", {})
    _changed_platform = False

    # enabled: 启用解析的平台勾选 (替代旧 27 模板 enable)
    # 注意: options 必须为纯字符串数组 (AstrBot 勾选列表按字符串渲染, 对象 → [object Object])
    _enabled = _pf_items.setdefault(
        "enabled",
        {"type": "list", "description": "启用解析的平台", "options": [], "default": []},
    )
    if _enabled.get("options") != _plats:
        _enabled["options"] = _plats
        _changed_platform = True
    if not _enabled.get("default"):
        _enabled["default"] = list(_plats)
        _changed_platform = True

    # T2: proxied (规则代理) 已移除 — 无 IP:端口填写入口, 代理体系收敛为直连
    if "proxied" in _pf_items:
        _pf_items.pop("proxied", None)
        _changed_platform = True

    # 动态源: 源码支持 cookie 的平台 (Config 中 plite_<platform>_ck 字段)
    _ck_platforms = []
    _ck_labels = {}
    for _fname in _UpConfig.model_fields:
        if _fname.startswith("plite_") and _fname.endswith("_ck"):
            _plat = _fname[len("plite_") : -len("_ck")]
            _ck_platforms.append(_plat)
            _ck_labels[_plat] = tr(_fname)

    # cookies: 动态模板列表 (平台下拉仅含支持 cookie 的源码平台, 纯字符串 options)
    if _ck_platforms:
        _ck_desc = "平台 Cookie (自动同步至解析器)" + (
            f"; 支持平台: {', '.join(_ck_labels.get(p, p) for p in _ck_platforms)}"
            if _ck_platforms
            else ""
        )
        _cookies = _pf_items.setdefault(
            "cookies",
            {
                "type": "template_list",
                "description": _ck_desc,
                "templates": {
                    "default": {
                        "name": "平台Cookie",
                        "items": {
                            "platform": {
                                "type": "list",
                                "description": "平台",
                                "options": [],
                                "default": [],
                            },
                            "cookie": {
                                "type": "string",
                                "description": "Cookie",
                                "default": "",
                            },
                        },
                    }
                },
            },
        )
        _c_tpl = _cookies.setdefault("templates", {}).setdefault("default", {})
        _c_platform = _c_tpl.setdefault("items", {}).setdefault("platform", {})
        if _c_platform.get("options") != _ck_platforms:
            _c_platform["options"] = _ck_platforms
            _changed_platform = True

    if _changed_platform:
        updated = True
        injected.append("platforms")

    # 1) features: bool 字段 (options 恒为当前扫描值, default 保留用户勾选)
    bool_fields = sorted(
        k
        for k, f in _UpConfig.model_fields.items()
        if is_bool_field(f) and k.startswith("plite_")
    )
    _features = schema.setdefault(
        "features", {"type": "list", "options": [], "default": []}
    )
    _new_opts = [label(k) for k in bool_fields]
    if _features.get("options") != _new_opts:
        _features["options"] = _new_opts
        updated = True
        injected.append("features")
    if _inject_new and not _features.get("default"):
        # 首次注入: 初始化默认勾选
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
        is_bool = is_bool_field(finfo)
        is_enum = is_enum_field(finfo)
        if is_bool or is_enum:
            continue  # bool → features; enum → parser_extra
        if (
            fname in schema
            and schema[fname] != ["__INJECT__"]
            and not isinstance(schema[fname], list)
        ):
            if schema[fname].get("description") != tr(fname):
                schema[fname]["description"] = tr(
                    fname
                )  # 描述恒等于翻译 (升级/翻译更新生效)
                updated = True
            if _inject_new:
                default = finfo.default
                if default is not None and not isinstance(
                    default, (int, float, str, bool, list, dict)
                ):
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
    for bf in _BRIDGE_FIELDS:
        parts = bf["path"].split(".")
        obj = schema
        for p in parts[:-1]:
            obj = obj.setdefault(
                p,
                {}
                if p != parts[-2]
                else (obj.get(p, {}) if isinstance(obj.get(p), dict) else {}),
            )
        last = parts[-1]
        existing = obj.get(last, {})
        needs_inject = (
            isinstance(existing, dict) and existing.get("options") == ["__INJECT__"]
        ) or (not isinstance(existing, dict) or not existing)
        if _inject_new and needs_inject:
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

    # 4) plite_disabled_platforms (T1: 移除 — 与 platforms.items.enabled 收敛,
    #    旧 schema/配置残留键清理, 防 AstrBot 陈旧残留显示)
    if "plite_disabled_platforms" in schema:
        schema.pop("plite_disabled_platforms", None)
        updated = True

    # 5) parser_extra: 枚举字段 (description 恒翻译)
    _PARSER_EXTRA_MAP.clear()
    extra = {}
    _pe_obj = schema.get("parser_extra")
    for fname, finfo in _UpConfig.model_fields.items():
        if not is_enum_field(finfo):
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
        _entry = {
            "description": tr(fname),
            "type": "string" if not is_list else "list",
            "options": list(enum_cls.__members__),
            "default": fallback,
            "hint": "",
        }
        if _pe_obj and isinstance(_pe_obj, dict):
            _old = (_pe_obj.get("items") or {}).get(short_key)
            if isinstance(_old, dict) and "default" in _old:
                _entry["default"] = _old["default"]  # 保留用户值
        extra[short_key] = _entry  # 描述恒翻译, 全量同步
    if extra:
        schema.setdefault(
            "parser_extra",
            {"type": "object", "description": tr("parser_extra"), "items": {}},
        )
        schema["parser_extra"]["items"] = {
            **(schema.get("parser_extra", {}).get("items") or {}),
            **extra,
        }
        updated = True
        injected.append("parser_extra")

    # 5.5) 清理: 已删自研字段残留 (r8/r9 删除的模块对应键, 防 AstrBot 陈旧残留)
    for _stale in _STALE_CONFIG_KEYS:
        if _stale in schema:
            schema.pop(_stale, None)
            updated = True
    # platforms.items 内 proxied 残留清理
    _pfi = (
        schema.get("platforms", {}).get("items")
        if isinstance(schema.get("platforms"), dict)
        else None
    )
    if isinstance(_pfi, dict) and "proxied" in _pfi:
        _pfi.pop("proxied", None)
        updated = True

    if updated or not flag_path.exists():
        # 顺序: 注入的 standalone 源码实现配置项在前 (上游模型序), 扩展自实现配置项在后
        _ordered = {}
        _up_keys = [
            k for k in _UpConfig.model_fields if k in schema or k.startswith("plite_")
        ]
        _known_order = [
            *[k for k in _up_keys if k in schema],
            "parser_extra",
            "platforms",
        ]
        _seen = set()
        for _key in _known_order:
            if _key in schema and _key not in _seen:
                _ordered[_key] = schema[_key]
                _seen.add(_key)
        for _key in _BRIDGE_PATHS:  # 扩展字段固定序 (声明序, 语义分组)
            if _key in schema and _key not in _seen:
                _ordered[_key] = schema[_key]
                _seen.add(_key)
        for _k, _v in schema.items():  # 其余 (未知新增) 保持稳定
            if _k not in _seen:
                _ordered[_k] = _v
        schema = _ordered
        # P1-8: 多实例并发启动时 schema 文件原子写 (tmp + os.replace)
        _schema_tmp = schema_path.with_name(schema_path.name + ".tmp")
        _schema_tmp.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), "utf-8"
        )
        import os as _os

        _os.replace(_schema_tmp, schema_path)
        _flag_tmp = flag_path.with_name(flag_path.name + ".tmp")
        _flag_tmp.write_text(str(SCHEMA_VERSION))
        _os.replace(_flag_tmp, flag_path)
        _logger.info(
            f"[ParserLite] schema injected: {', '.join(injected) if injected else '(defaults sync)'}"
        )
    return injected
