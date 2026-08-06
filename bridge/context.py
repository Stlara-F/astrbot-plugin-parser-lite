"""桥接上下文: 上游引用聚合 + BridgeConfig 单例.

薄桥接核心: 所有对上游 (nonebot_plugin_parser_lite) 的引用在此聚合,
bridge 其余模块只经此访问上游, 上游不反向依赖 bridge.
"""

from __future__ import annotations

import hashlib
import json
import sys
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
        from bridge.render_patch import apply_render_patch

        apply_render_patch()
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


# ── 字段标签 (features 双向映射用) ───────────────────────────────────────────

def _is_bool_annotation(ann) -> bool:
    """bool 注解判定 (兼容 Optional[bool] / bool | None)."""
    if ann is bool:
        return True
    if hasattr(ann, "__args__"):
        return bool in ann.__args__
    return False


def label(k: str) -> str:
    # 翻译表优先; 未翻译字段回退英文驼峰 (未翻译状态, 新增字段可见)
    try:
        from bridge.inject import tr as _tr

        return _tr(k)
    except Exception:
        pass
    return label_en(k)


def label_en(k: str) -> str:
    """英文驼峰标签 (旧配置 features 值兼容)."""
    s = k.removeprefix("plite_").replace("_", " ")
    if s.startswith("bili "):
        s = "B站" + s[4:]
    return " ".join(w[0].upper() + w[1:] for w in s.split())


# ── BridgeConfig 单例 ────────────────────────────────────────────────────────

class BridgeConfig:
    _instance: Any = None
    _source: dict | None = None
    _hash: str = ""

    @classmethod
    def configure(cls, _config: dict | None = None, **kwargs):
        from bridge.proxy import apply_downloader_proxy
        from bridge.resolve import read_proxy_config

        _UpConfig = up_config()
        data = {**(_config or getattr(cls, "_source", {}) or {}), **kwargs}
        if _config is not None:
            cls._source = _config
        elif kwargs:
            cls._source = data  # AstrBot configure(**self.config) 走此分支
        # features 标签 → plite_* bool 反向映射 (兼容中英文旧值 + Optional[bool])
        features_list = data.get("features", [])
        if isinstance(features_list, list):
            for k, f in _UpConfig.model_fields.items():
                if _is_bool_annotation(f.annotation) and k.startswith("plite_"):
                    data[k] = (label(k) in features_list) or (label_en(k) in features_list)
        valid = {k: v for k, v in data.items() if k in _UpConfig.model_fields}
        # parser_extra 冲突覆盖: 注入到 valid 中 (优先于顶级 plite_ 同名字段)
        cls._inject_parser_extra(valid, data)
        if not valid:
            return
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
        dl = up_downloader()
        dl.MAX_RETRIES = _cfg.max_retries
        if hasattr(dl, "max_size_mb"):
            dl.max_size_mb = _cfg.max_size
        apply_downloader_proxy(read_proxy_config())
        try:
            from astrbot.api import logger as _alog
        except Exception:
            import logging
            _alog = logging.getLogger("parser-lite.bridge.context")
        _alog.debug(f"[ParserLite] configure: {len(valid)} fields, dirty={h != cls._hash}")

    @classmethod
    def _inject_parser_extra(cls, valid: dict, data: dict):
        """将 parser_extra 嵌套表的值解析后写入 valid (覆盖同名字段冲突)"""
        try:
            from bridge.inject import get_parser_extra_mapping  # 解耦: 注入层提供映射
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
                valid[pconfig_field] = [enum_cls[v] for v in val if v in enum_cls.__members__]

    @classmethod
    def get_config(cls):
        if cls._instance is None:
            cls.configure()
        return cls._instance


configure = BridgeConfig.configure
get_config = BridgeConfig.get_config
