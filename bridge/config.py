"""配置适配 (bridge-core 超薄桥接: config + inject + i18n + state 合并).

- read_cfg/module_cfg/global_source/bridge_cfg: 配置读取唯一入口
- BridgeConfig: AstrBot 配置单例 (schema 驱动热载 → 上游 configure)
- inject_dynamic_options_static: WebUI schema 注入 (0 硬编码决策树)
- JsonStateStore + disabled_groups 持久化: 群禁用状态
- tr/label: 翻译与 features 标签 (i18n 单一来源)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import typing
from typing import Any

# ── i18n (翻译/标签) ─────────────────────────────────────────────
# ── 配置项翻译列表: 注入字段 → 中文标签 ─────────────────────────────────────
# 未翻译的 key 原样显示 (新增字段以未翻译状态进入配置, 后续在此补充即可)
TRANSLATIONS: dict[str, str] = {
    "features": "功能开关",
    "platforms": "平台配置",
    "parser_extra": "平台解析扩展",
    "send_strategy": "发送策略",
    "plite_direct_link": "直链免下载",
    "plite_send_cover_only": "视频仅发封面",
    "plite_video_file_threshold_mb": "视频文件发送阈值MB",
    "plite_forward_max_nodes": "合并转发最大节点数",
    "plite_need_upload": "上传音视频文件",
    "plite_need_upload_audio": "上传音频文件",
    "plite_need_upload_video": "上传视频文件",
    "plite_use_base64": "Base64编码发送",
    "plite_max_size": "资源最大大小MB",
    "plite_duration_maximum": "视频音频最大时长秒",
    "plite_append_url": "结果附加原始URL",
    "plite_append_qrcode": "结果附加原始URL二维码",
    "plite_blacklist_users": "黑名单用户",
    "plite_bili_video_codes": "B站视频编码",
    "plite_bili_video_quality": "B站视频清晰度",
    "plite_need_forward_contents": "合并转发内容",
    "plite_lazy_download": "懒下载模式",
    "plite_lazy_download_tip": "懒下载命令提示",
    "plite_lazy_download_timeout": "懒下载等待命令超时",
    "plite_download_command": "懒下载命令列表",
    "plite_browser_path": "浏览器程序路径",
    "plite_live_photo": "Live Photo转码",
    "plite_headless": "无头浏览器",
    "plite_max_comments": "最大评论数量",
    "plite_forward_text_threshold": "纯文本强制转发阈值",
    "plite_max_retries": "最大下载重试次数",
    "plite_day_range": "白天时间范围",
}


def tr(key: str) -> str:
    """翻译查找: 未翻译 key 原样返回 (新增字段以未翻译状态进入配置)."""
    return TRANSLATIONS.get(key, key)


def label_en(k: str) -> str:
    """英文驼峰标签 (旧配置 features 值兼容)."""
    s = k.removeprefix("plite_").replace("_", " ")
    if s.startswith("bili "):
        s = "B站" + s[4:]
    return " ".join(w[0].upper() + w[1:] for w in s.split())


def label(k: str) -> str:
    """字段标签: 翻译表优先; 未翻译回退英文驼峰 (新增字段可见)."""
    return tr(k) if k in TRANSLATIONS else label_en(k)


def is_bool_annotation(ann) -> bool:
    """bool 注解判定 (兼容 bool | None / Optional[bool] 联合注解)."""
    if ann is bool:
        return True
    if hasattr(ann, "__args__"):
        return bool in ann.__args__
    return False


def up_config():
    """上游 Config 类 (兼容别名: 测试/内部延迟引用)."""
    from nonebot_plugin_parser_lite.config import Config

    return Config


def read_cfg(source: dict | None, key: str, default: Any = None) -> Any:
    """从配置源读取值, 缺失或 None 回退默认.

    支持点路径嵌套: "platforms.enabled", "plite_max_size".
    注意: 0 是合法值 (如 TTL=0 表示禁用), 不被回退覆盖.
    """
    if not source:
        return default
    try:
        v: Any = source
        for part in key.split("."):
            if not isinstance(v, dict):
                return default
            v = v.get(part)
        return v if v is not None else default
    except Exception:
        return default


def module_cfg(source: dict | None, section: str, default: Any = None) -> Any:
    """提取模块配置段 (功能模块自包含: 每模块只读自己的 section).

    :param source: 配置源 (可注入; None/空 → 默认)
    :param section: 模块配置段名 (如 "platforms", "parser_extra")
    :param default: 段缺失/非 dict 时的默认
    """
    if not source:
        return default
    raw = source.get(section, default)
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception:
            return default
    if raw is None:
        return default
    return raw


def global_source() -> dict:
    """全局配置源 (依赖注入的默认来源)."""
    return BridgeConfig._source or {}


def bridge_cfg(key: str, default: Any = None) -> Any:
    """全局配置读取唯一入口 (业务代码统一走此函数).

    等价 read_cfg(global_source(), key, default) — 单一来源, 避免
    各模块直接散用 read_cfg/global_source/BridgeConfig._source.
    """
    return read_cfg(global_source(), key, default)


class BridgeConfig:
    _instance: Any = None
    _source: dict | None = None
    _hash: str = ""

    @classmethod
    def configure(cls, _config: dict | None = None, **kwargs):
        # R4: 统一从 adapter 导入 (平台/上游聚合)
        from bridge.adapter import apply_downloader_proxy

        _UpConfig = up_config()
        data = {**(_config or getattr(cls, "_source", {}) or {}), **kwargs}
        # B11: 保留 _source 单例身份 (clear+update), 避免外部缓存引用漂移
        if _config is not None:
            if cls._source is None:
                cls._source = {}
            cls._source.clear()
            cls._source.update(_config)
        elif kwargs:
            if cls._source is None:
                cls._source = {}
            cls._source.update({k: v for k, v in kwargs.items() if k != "__hash__"})
        # features 标签 → plite_* bool 反向映射 (兼容中英文旧值 + bool | None)
        features_list = data.get("features", [])
        if isinstance(features_list, list):
            for k, f in _UpConfig.model_fields.items():
                if is_bool_annotation(f.annotation) and k.startswith("plite_"):
                    data[k] = (label(k) in features_list) or (
                        label_en(k) in features_list
                    )
        valid = {k: v for k, v in data.items() if k in _UpConfig.model_fields}
        # parser_extra 冲突覆盖: 注入到 valid 中 (优先于顶级 plite_ 同名字段)
        cls._inject_parser_extra(valid, data)
        if not valid:
            return
        s = json.dumps(
            {
                k: (
                    v.name
                    if hasattr(v, "name")
                    else [e.name for e in v]
                    if isinstance(v, list) and v and hasattr(v[0], "name")
                    else v
                )
                for k, v in valid.items()
            },
            sort_keys=True,
        )
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
            for key in (
                cfg_mod,
                f"nonebot_plugin_parser_lite.{cfg_mod}"
                if "." not in cfg_mod
                else cfg_mod,
            ):
                mod = sys.modules.get(key)
                if mod is not None:
                    mod.pconfig = _cfg
                    break
        cls._instance = _cfg
        from bridge.adapter import up_downloader

        dl = up_downloader()
        dl.MAX_RETRIES = _cfg.max_retries
        if hasattr(dl, "max_size_mb"):
            dl.max_size_mb = _cfg.max_size
        # T2: 代理体系已收敛直连; 重建 DOWNLOADER 客户端 (插件重载后残留清理)
        apply_downloader_proxy("")
        try:
            from astrbot.api import logger as _alog
        except Exception:
            import logging

            _alog = logging.getLogger("parser-lite.bridge.adapter")
        _alog.debug(
            f"[ParserLite] configure: {len(valid)} fields, dirty={h != cls._hash}"
        )

    @classmethod
    def _inject_parser_extra(cls, valid: dict, data: dict):
        """将 parser_extra 嵌套表的值解析后写入 valid (覆盖同名字段冲突)"""
        try:
            from bridge.config import get_parser_extra_mapping

            mapping = get_parser_extra_mapping()
        except Exception:
            mapping = {}
        extra = data.get("parser_extra", {})
        if not extra or not isinstance(extra, dict):
            return
        for ast_key, (pconfig_field, enum_cls, is_list) in mapping.items():
            val = extra.get(ast_key)
            if val is None:
                continue
            if isinstance(val, str):
                # 单选项: 直接传字符串 "_1080P" 或枚举成员名
                if not is_list and val in enum_cls.__members__:
                    valid[pconfig_field] = enum_cls[val]
                # 多选项: JSON 数组字符串 "["AVC","AV1"]"
                elif is_list and val.strip().startswith("["):
                    val = json.loads(val)
            if isinstance(val, list) and is_list:
                valid[pconfig_field] = [
                    enum_cls[v] for v in val if v in enum_cls.__members__
                ]

    @classmethod
    def get_config(cls):
        if cls._instance is None:
            cls.configure()
        return cls._instance


configure = BridgeConfig.configure
get_config = BridgeConfig.get_config


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
def _lazy_get_sendable_types() -> list[str]:
    """发送类型动态扫描 (延迟 import 防 config↔send 循环)."""
    from bridge.send import get_sendable_types

    return get_sendable_types()


_BRIDGE_FIELDS: list[dict] = [
    {
        "path": "send_strategy",
        "type": "list",
        "desc": tr("send_strategy"),
        "default": _lazy_get_sendable_types,
        "options": _lazy_get_sendable_types,
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
    from nonebot_plugin_parser_lite.config import Config as _UpConfig

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
    from nonebot_plugin_parser_lite.parsers.base import BaseParser

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


class JsonStateStore:
    def __init__(
        self,
        path: str | Path | None = None,
        flush_every: int = 10,
        flush_interval: float = 5.0,
    ):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {}
        self._mutations = 0
        self._last_flush = time.time()  # 构造时起算, 避免首次 update 误判超时
        self._flush_every = max(flush_every, 1)
        self._flush_interval = max(flush_interval, 0.5)
        self._load()

    @property
    def data(self) -> dict:
        """共享数据视图 (调用方直接读; 变更须经 update)."""
        return self._data

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception:
            self._data = {}

    def _flush_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _tmp = self._path.with_name(self._path.name + ".tmp")
            _tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(_tmp, self._path)  # 原子替换, 崩溃不损坏
            self._last_flush = time.time()
            self._mutations = 0
        except Exception:
            pass

    def update(self, fn) -> None:
        """锁内执行变更 fn(data) + 写节流落盘."""
        if self._path is None:
            fn(self._data)
            return
        with self._lock:
            fn(self._data)
            self._mutations += 1
            now = time.time()
            if (
                self._mutations >= self._flush_every
                or now - self._last_flush >= self._flush_interval
            ):
                self._flush_locked()

    def flush(self) -> None:
        """显式落盘 (进程退出/命令触发)."""
        if self._path is None:
            return
        with self._lock:
            self._flush_locked()

    def reset(self, data: dict | None = None) -> None:
        """清空/替换 (测试用)."""
        with self._lock:
            self._data = dict(data) if data is not None else {}
            self._mutations = 0


def get_base_dir() -> Path:
    """解析基础目录: PARSER_LITE_BASE_DIR 优先, 默认 cwd/.parser-lite (与上游一致)."""
    return Path(
        os.environ.get("PARSER_LITE_BASE_DIR") or (Path.cwd() / ".parser-lite")
    ).resolve()


def state_dir() -> Path:
    """bridge 运行时状态目录 (禁用群组等 JSON 状态)."""
    return get_base_dir() / "parser_lite"


def ensure_state_dir() -> Path:
    _d = state_dir()
    _d.mkdir(parents=True, exist_ok=True)
    return _d


_DISABLED_GROUPS_FILE = None  # 延迟解析 (统一路径, 消除 __file__ 环境差异)
_DISABLED_GROUPS_STORE = None  # JsonStateStore (B6: 统一锁/原子写/节流)


def _disabled_groups_path() -> Path:
    global _DISABLED_GROUPS_FILE
    if _DISABLED_GROUPS_FILE is None:
        ensure_state_dir = globals()["ensure_state_dir"]

        _DISABLED_GROUPS_FILE = ensure_state_dir() / "disabled_groups.json"
    return _DISABLED_GROUPS_FILE


def _disabled_groups_store():
    """禁用群组状态存储 (JsonStateStore: 锁 + 原子写 + 节流)."""
    global _DISABLED_GROUPS_STORE
    if _DISABLED_GROUPS_STORE is None:
        JsonStateStore = globals()["JsonStateStore"]

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
    """检测 Chromium 缺失系统库 (仅 Linux, Windows/macOS 不误报)."""
    if sys.platform != "linux":
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
