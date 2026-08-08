#!/usr/bin/env python3
"""本地调试管线 — 脱离 AstrBot 运行完整解析, 打印结构与媒体清单.

用法:
  python run_local.py "https://www.bilibili.com/video/BV1iKgv6HEgJ"
  python run_local.py --url <url> [--raw] [--no-cards]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
os.environ.setdefault("PARSER_LITE_STANDALONE", "1")
os.environ.setdefault("PARSER_LITE_BASE_DIR", str(_HERE / ".parser-lite-test"))
_src = str(_HERE / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


async def main() -> int:
    ap = argparse.ArgumentParser(description="ParserLite 本地调试")
    ap.add_argument("url", nargs="?", help="要解析的 URL")
    ap.add_argument("--url", dest="url_opt", help="URL (备用参数名)")
    ap.add_argument("--raw", action="store_true", help="打印原始 ParseResult 字段")
    ap.add_argument("--no-cards", action="store_true", help="跳过渲染卡片验证")
    args = ap.parse_args()

    url = args.url or args.url_opt
    if not url:
        print("用法: python run_local.py <URL>")
        return 1

    from bridge.fixtures import install_httpx_hook  # noqa: E402
    from bridge.send import format_brief, format_full  # noqa: E402
    from main import ParserLite  # noqa: E402  (需先设 env)
    from nonebot_plugin_parser_lite.parsers.base import BaseParser  # noqa: E402

    # 录制/回放钩子 (PARSER_LITE_RECORD_DIR / PARSER_LITE_REPLAY 动态控制)
    install_httpx_hook()

    print("== ParserLite 本地调试 ==")
    print(f"URL: {url}")
    print(f"Parsers: {len(BaseParser.get_all_subclass())}")

    p = ParserLite()
    try:
        result = await p.parse_url(url)
        print("\n-- 解析结果 --")
        print(format_full(result))
        print("\n-- 摘要 --")
        print(format_brief(result))

        if args.raw:
            print("\n-- 原始字段 --")
            try:
                print(
                    json.dumps(
                        result.model_dump(), ensure_ascii=False, indent=2, default=str
                    )[:4000]
                )
            except Exception as e:
                print(f"(model_dump 失败: {e})")

        media = [c for c in result.content if hasattr(c, "path_task")]
        if media:
            print(f"\n-- 媒体清单 ({len(media)} 项) --")
            for c in media:
                url_s = getattr(getattr(c, "path_task", None), "url", "")
                print(f"  {type(c).__name__}: {str(url_s)[:100]}")
        else:
            print("\n(无媒体项)")

        if not args.no_cards:
            try:
                from nonebot_plugin_parser_lite.render import RENDERER

                data = await RENDERER.resolve_parse_result(result)
                print(f"\n-- 渲染数据 -- keys={sorted(data.keys())}")
            except Exception as e:
                print(f"\n(渲染数据失败: {e})")
        return 0
    except Exception as e:
        import traceback

        print(f"\n✗ 解析失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    finally:
        try:
            await p.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
