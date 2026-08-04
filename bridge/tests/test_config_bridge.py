"""config_bridge 相关测试 — 验证 _bridge_cfg 回退与 bridge 字段注入.

覆盖:
- _bridge_cfg 缺失回退 / 有值返回 / 异常安全
- 新增可配置项 (dedup_ttl/cache_interval/compress_mb/forward_nodes) 读取得当
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402


def test_bridge_cfg_missing_returns_default():
    main.BridgeConfig._source = {}
    assert main._bridge_cfg("plite_dedup_ttl", 60) == 60


def test_bridge_cfg_value_returned():
    main.BridgeConfig._source = {"plite_dedup_ttl": 120}
    assert main._bridge_cfg("plite_dedup_ttl", 60) == 120


def test_bridge_cfg_none_value_uses_default():
    main.BridgeConfig._source = {"plite_dedup_ttl": None}
    assert main._bridge_cfg("plite_dedup_ttl", 60) == 60


def test_bridge_cfg_zero_is_respected():
    # 0 是合法值 (如 TTL=0 表示不去重), 不应被回退覆盖
    main.BridgeConfig._source = {"plite_dedup_ttl": 0}
    assert main._bridge_cfg("plite_dedup_ttl", 60) == 0


def test_bridge_fields_contains_new_keys():
    paths = [f["path"] for f in main._BRIDGE_FIELDS]
    for key in ("plite_dedup_ttl", "plite_cache_interval",
                "plite_image_compress_mb", "plite_forward_max_nodes",
                "plite_http_proxy", "send_strategy"):
        assert key in paths, f"缺少配置项 {key}"


def test_bridge_fields_no_duplicate_paths():
    paths = [f["path"] for f in main._BRIDGE_FIELDS]
    assert len(paths) == len(set(paths)), "存在重复配置路径"


def test_bridge_fields_order_priority_first():
    # 使用频率排序: 高频 (proxy/send_strategy) 应在前
    paths = [f["path"] for f in main._BRIDGE_FIELDS]
    assert paths[0] == "plite_http_proxy"
    assert paths[1] == "send_strategy"
    # 低频后台在最后
    assert paths[-1] in ("arbiter", "cookie_health")


def test_dynamic_options_source_callable():
    """parsers.items.proxied 的 source 是动态生成器 (非硬编码平台清单)."""
    entry = next(f for f in main._BRIDGE_FIELDS
                 if f["path"] == "parsers.items.proxied")
    assert callable(entry.get("source"))
    options = entry["source"]()
    assert isinstance(options, list)
    assert "bilibili" in options  # 动态扫描结果
