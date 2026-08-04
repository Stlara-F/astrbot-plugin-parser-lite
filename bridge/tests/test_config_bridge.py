"""config 读取与字段注入测试 — 无 astrbot 依赖 (CI 可跑).

覆盖:
- bridge.cfg.read_cfg 回退/取值/None/0 语义
- _BRIDGE_FIELDS 结构: 无重复、必含新键、使用频率排序
- 动态 options 生成器 (非硬编码平台清单)
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.cfg import read_cfg  # noqa: E402


def test_read_cfg_missing_returns_default():
    assert read_cfg({}, "plite_dedup_ttl", 60) == 60


def test_read_cfg_value_returned():
    assert read_cfg({"plite_dedup_ttl": 120}, "plite_dedup_ttl", 60) == 120


def test_read_cfg_none_uses_default():
    assert read_cfg({"plite_dedup_ttl": None}, "plite_dedup_ttl", 60) == 60


def test_read_cfg_zero_respected():
    # 0 是合法值 (TTL=0 表示不去重), 不应被回退覆盖
    assert read_cfg({"plite_dedup_ttl": 0}, "plite_dedup_ttl", 60) == 0


def test_read_cfg_none_source():
    assert read_cfg(None, "k", 1) == 1


def test_read_cfg_exception_safe():
    class Bad:
        def get(self, *a):
            raise RuntimeError("boom")

    assert read_cfg(Bad(), "k", 1) == 1


def test_bridge_fields_structure():
    """读取 main.py 的 _BRIDGE_FIELDS 结构 (静态解析, 不 import main)."""
    src = (_ROOT / "main.py").read_text("utf-8")
    start = src.find("_BRIDGE_FIELDS: list[dict] = [")
    end = src.find('"""AstrBot', start)
    assert start != -1
    assert end != -1
    block = src[start:end]

    import re

    paths = re.findall(r'"path": "([^"]+)"', block)
    assert paths, "未解析到配置路径"

    # 必含新键
    for key in ("plite_dedup_ttl", "plite_cache_interval",
                "plite_image_compress_mb", "plite_forward_max_nodes",
                "plite_http_proxy", "send_strategy"):
        assert key in paths, f"缺少配置项 {key}"

    # 无重复
    assert len(paths) == len(set(paths)), "存在重复配置路径"

    # 使用频率排序: 高频在前
    assert paths[0] == "plite_http_proxy"
    assert paths[1] == "send_strategy"
    assert paths[-1] in ("arbiter", "cookie_health")


def test_bridge_fields_dynamic_source():
    """parsers.items.proxied 的 source 是动态生成器 (非硬编码平台清单)."""
    src = (_ROOT / "main.py").read_text("utf-8")
    assert "BaseParser.get_all_subclass()" in src
    assert '"path": "parsers.items.proxied"' in src
