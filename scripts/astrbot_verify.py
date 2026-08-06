# -*- coding: utf-8 -*-
"""AstrBot 实机验证脚本: 生成配置检查 + doctor 自检.

用法: python -X utf8 scripts/astrbot_verify.py [astrbot_test_dir]
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    # 1. AstrBot 生成配置
    cfg_path = base / "data" / "config" / "astrbot_plugin_parser_lite_config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        print(f"[OK] AstrBot 生成配置: {len(cfg)} 键")
        print(f"     send_strategy: {cfg.get('send_strategy')}")
        print(f"     platforms 条数: {len(cfg.get('platforms') or [])}")
    else:
        print(f"[SKIP] 未找到 {cfg_path} (插件未加载?)")

    # 2. 注入 schema
    schema_path = (
        base / "data" / "plugins" / "astrbot_plugin_parser_lite" / "_conf_schema.json"
    )
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        print(f"[OK] schema 注入: {len(schema)} 字段")
        bad = [
            k
            for k, v in schema.items()
            if isinstance(v, dict) and v.get("type") == "object" and "items" not in v
        ]
        print(f"     object 缺 items: {bad if bad else '无'}")
    else:
        print(f"[SKIP] 未找到 {schema_path}")

    # 3. doctor 自检
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ["PARSER_LITE_STANDALONE"] = "1"
    os.environ["PARSER_LITE_BASE_DIR"] = str(base / "data")

    import asyncio  # noqa: E402

    from bridge.doctor import run_checks, summarize  # noqa: E402

    async def go():
        results = await run_checks()
        s = summarize(results)
        print(
            f"[doctor] {s['ok']}/{s['total']} OK, {s['warn']} warn, {s['failed']} fail"
        )
        for r in results:
            icon = "OK" if r.ok else ("WARN" if r.warn else "FAIL")
            print(f"  [{icon}] {r.name}: {r.detail[:60]}")

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    sys.exit(main())
