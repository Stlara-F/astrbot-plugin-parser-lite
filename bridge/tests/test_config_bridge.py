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
    assert read_cfg({}, "plite_max_size", 90) == 90


def test_read_cfg_value_returned():
    assert read_cfg({"plite_max_size": 120}, "plite_max_size", 90) == 120


def test_read_cfg_none_uses_default():
    assert read_cfg({"plite_max_size": None}, "plite_max_size", 90) == 90


def test_read_cfg_zero_respected():
    # 0 是合法值 (TTL=0 表示不去重), 不应被回退覆盖
    assert read_cfg({"plite_max_size": 0}, "plite_max_size", 90) == 0


def test_read_cfg_none_source():
    assert read_cfg(None, "k", 1) == 1


def test_read_cfg_exception_safe():
    class Bad:
        def get(self, *a):
            raise RuntimeError("boom")

    assert read_cfg(Bad(), "k", 1) == 1


def _bridge_fields_src():
    """_BRIDGE_FIELDS 现定义于 bridge/inject.py (main.py 薄化)."""
    return (_ROOT / "bridge" / "inject.py").read_text("utf-8")


def test_bridge_fields_structure():
    """读取 bridge/inject.py 的 _BRIDGE_FIELDS 结构 (静态解析)."""
    src = _bridge_fields_src()
    start = src.find("_BRIDGE_FIELDS: list[dict] = [")
    end = src.find("_PARSER_EXTRA_MAP", start)
    assert start != -1
    assert end != -1
    block = src[start:end]

    import re

    paths = re.findall(r'"path": "([^"]+)"', block)
    assert paths, "未解析到配置路径"

    # 必含新键 (r8/r10: dedup/cache_interval/image_compress_mb 已删)
    for key in (
        "plite_forward_max_nodes",
        "send_strategy",
    ):
        assert key in paths, f"缺少配置项 {key}"

    # 无重复
    assert len(paths) == len(set(paths)), "存在重复配置路径"

    # 修改频率排序: 高频 (发送策略) 在前 (platforms 动态注入不在此列表)
    assert paths[0] == "send_strategy"
    assert paths[1] == "plite_direct_link"
    assert (
        paths[-1] == "plite_forward_max_nodes"
    )  # r8: 自研字段已删, 最后为发送适配字段


def test_bridge_fields_no_deprecated():
    """已废弃 parsers.items (cookies/proxied) 不再注入 — 统一 platforms."""
    src = _bridge_fields_src()
    assert '"path": "parsers.items.cookies"' not in src
    assert '"path": "parsers.items.proxied"' not in src
    # platforms 统一勾选列表 (enabled/proxied) + cookies 动态模板 (纯字符串 options)
    # T2: proxied 已移除
    assert "_proxied = _pf_items.setdefault(" not in src
    assert "_enabled = _pf_items.setdefault(" in src
    assert '"type": "template_list"' in src
    assert "平台 Cookie" in src
    # options 必须纯字符串 (AstrBot 勾选列表按字符串渲染, 对象 → [object Object])
    assert '{"value": "' not in src


def test_object_fields_have_items():
    """AstrBot 兼容: object 类型配置必须含 items (否则 _parse_schema KeyError: 'items').

    回归保护: 实机曾报 'items' KeyError (push/delay_send/arbiter/cookie_health 缺 items).
    """
    src = _bridge_fields_src()

    # 静态检查 _BRIDGE_FIELDS: 每个 object 类型条目必须有 "items" 或 "items_type"
    start = src.find("_BRIDGE_FIELDS: list[dict] = [")
    end = src.find("_PARSER_EXTRA_MAP", start)
    block = src[start:end]

    # 按条目切分
    entries = []
    depth = 0
    pos = block.find("{")
    while pos != -1:
        cursor = pos
        depth = 0
        while cursor < len(block):
            ch = block[cursor]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    entries.append(block[pos : cursor + 1])
                    break
            cursor += 1
        pos = block.find("{", cursor + 1)

    objects = [e for e in entries if '"type": "object"' in e]
    # T3: delay_send/arbiter/cookie_health 移除后可能无 object 类型; 有则必须含 items
    for e in objects:
        assert '"items"' in e or '"items_type"' in e, f"object 配置缺 items: {e[:80]}"
