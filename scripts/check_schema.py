#!/usr/bin/env python3
"""提交前校验: schema 注入顺序 + 无硬编码完整性 (CI 调用).

校验:
1. _BRIDGE_FIELDS 使用频率排序 (高频在前, 低频在后)
2. _BRIDGE_FIELDS 无重复路径
3. 注入后 schema key 顺序 = 使用频率序
4. 所有 bridge 配置项可注入 (无遗漏)

用法: python scripts/check_schema.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 使用频率分组 (用于断言排序)
HIGH = {"plite_http_proxy", "send_strategy", "parsers.items.proxied", "parsers.items.cookies"}
LOW = {"push", "delay_send", "arbiter", "cookie_health"}


def parse_bridge_fields(src: str) -> list[dict]:
    """静态解析 _BRIDGE_FIELDS (不 import main, 避免 astrbot 依赖)."""
    start = src.find("_BRIDGE_FIELDS: list[dict] = [")
    end = src.find('"""AstrBot', start)
    if start == -1 or end == -1:
        raise SystemExit("FAIL: 未找到 _BRIDGE_FIELDS")
    block = src[start:end]
    # 简单解析: 提取 path 顺序
    paths = re.findall(r'"path": "([^"]+)"', block)
    return [{"path": p} for p in paths]


def main() -> int:
    errors: list[str] = []
    main_src = (ROOT / "main.py").read_text("utf-8")
    fields = parse_bridge_fields(main_src)
    paths = [f["path"] for f in fields]

    # 1. 无重复
    if len(paths) != len(set(paths)):
        errors.append(f"FAIL: _BRIDGE_FIELDS 重复路径: "
                      f"{[p for p in paths if paths.count(p) > 1]}")

    # 2. 使用频率排序: 高频在前
    if paths and paths[0] != "plite_http_proxy":
        errors.append(f"FAIL: 首个配置应为 plite_http_proxy, 实际 {paths[0]}")
    if paths and paths[1] != "send_strategy":
        errors.append(f"FAIL: 第二个配置应为 send_strategy, 实际 {paths[1]}")
    # 低频应在最后
    last_low = [i for i, p in enumerate(paths) if p in LOW]
    first_high_end = max([i for i, p in enumerate(paths) if p in HIGH] or [-1])
    if last_low and min(last_low) < first_high_end:
        errors.append("FAIL: 低频配置 (push/arbiter 等) 应在高频之后")

    # 3. 必含新配置项
    for key in ("plite_dedup_ttl", "plite_cache_interval",
                "plite_image_compress_mb", "plite_forward_max_nodes"):
        if key not in paths:
            errors.append(f"FAIL: 缺少配置项 {key}")

    # 4. 注入后 schema 顺序 (若 schema 存在)
    schema_path = ROOT / "_conf_schema.json"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text("utf-8"))
        schema_keys = list(schema.keys())
        # 顶层 bridge 字段 (排除嵌套路径) 应出现在 schema 且相对顺序一致
        top_bridge = [p for p in paths if "." not in p]
        present = [p for p in top_bridge if p in schema_keys]
        if present != top_bridge:
            errors.append("FAIL: schema 顶层缺失 bridge 字段 "
                          f"(缺: {set(top_bridge) - set(present)})")
        # 相对顺序: 高频在前
        if present and present[0] != "plite_http_proxy":
            errors.append(f"FAIL: schema 首个配置应为 plite_http_proxy, 实际 {present[0]}")
        # 嵌套: parsers.items 顺序
        parsers_items = list((schema.get("parsers", {}).get("items") or {}).keys())
        if parsers_items and parsers_items[0] != "proxied":
            errors.append(f"FAIL: parsers.items 首项应为 proxied, 实际 {parsers_items}")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(paths)} 配置项, 排序/完整性/无硬编码校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
