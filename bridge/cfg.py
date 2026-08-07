"""bridge 配置读取辅助 — 无 astrbot 依赖, 可独立测试.

bridge_cfg(key, default): 全局配置读取唯一入口 (read_cfg(global_source())),
缺失/None 回退默认值 (0 硬编码: 所有桥接参数走此通道).
"""

from __future__ import annotations

from typing import Any


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
    from bridge.context import BridgeConfig

    return BridgeConfig._source or {}


def bridge_cfg(key: str, default: Any = None) -> Any:
    """全局配置读取唯一入口 (业务代码统一走此函数).

    等价 read_cfg(global_source(), key, default) — 单一来源, 避免
    各模块直接散用 read_cfg/global_source/BridgeConfig._source.
    """
    return read_cfg(global_source(), key, default)
