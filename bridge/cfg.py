"""bridge 配置读取辅助 — 无 astrbot 依赖, 可独立测试.

_bridge_cfg(key, default): 从 BridgeConfig._source 读取 bridge 语义配置,
缺失/None 回退默认值 (0 硬编码: 所有桥接参数走此通道).
"""

from __future__ import annotations

from typing import Any


def read_cfg(source: dict | None, key: str, default: Any = None) -> Any:
    """从配置源读取值, 缺失或 None 回退默认.

    注意: 0 是合法值 (如 TTL=0 表示禁用), 不被回退覆盖.
    """
    if not source:
        return default
    try:
        v = source.get(key)
        return v if v is not None else default
    except Exception:
        return default
