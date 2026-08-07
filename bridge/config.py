"""配置适配 (R3: 镜像上游 config/ 分层 — cfg 读取 + AstrBot 配置单例).

- read_cfg/module_cfg/global_source/bridge_cfg: 配置读取唯一入口
  (bridge_cfg(key, default) 全局业务入口, 0 硬编码)
- BridgeConfig: AstrBot 配置单例 (schema 驱动热载 → 上游 configure)
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from bridge.i18n import (
    is_bool_annotation as _is_bool_annotation,
)
from bridge.i18n import (
    label,
    label_en,
)


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
        # R4: 统一从 proxy 导入 (不绕路 resolve)
        from bridge.context import up_config
        from bridge.platform import apply_downloader_proxy

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
                if _is_bool_annotation(f.annotation) and k.startswith("plite_"):
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
        from bridge.context import up_downloader

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

            _alog = logging.getLogger("parser-lite.bridge.context")
        _alog.debug(
            f"[ParserLite] configure: {len(valid)} fields, dirty={h != cls._hash}"
        )

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
