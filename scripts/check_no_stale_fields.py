#!/usr/bin/env python3
"""r9 门禁: 已删自研符号零回潮断言 (防多轮审计反复发现残留).

扫描 bridge/ (生产代码)、main.py、_conf_schema.json、scripts/、run_local.py,
断言已删符号不再出现. 豁免: bridge/tests (历史断言)、src/ (上游).

用法: python scripts/check_no_stale_fields.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 已删自研符号 (r7/r8 删除的模块与字段)
STALE_SYMBOLS = [
    "push",
    "rate_limit",
    "debounce",
    "media_cache",
    "card_semantic",
    "custom_parser",
    "cookie_health",
    "delay_send",
    "arbiter",
    "LazyManager",
    "_RESULT_CACHE",
    "FEATURE_TABLE",
    "proxied",
    "plite_http_proxy",
    "plite_dedup_ttl",
    "plite_md5_",
    "plite_cache_interval",
    "plite_disabled_platforms",
    "test_urls",
    "push_interval",
    "_resolve_proxy_url",
    "_use_proxy_for",
    "_on_download_trigger",
    "on_url_auto",
    "cmd_parse_dl",
    "_inject_dynamic_options_static",
]

# 白名单: 语义化的历史/文档上下文 (集中声明点/注释引用/清理逻辑)
ALLOW_CONTEXTS = (
    "r7",
    "r8",
    "r9",  # 重构注释引用
    "T1",
    "T2",
    "T3",  # 重构段注释
    "已删",
    "已移除",
    "自研",  # 中文清理注释
    "残留清理",  # inject 清理段
    "stale",
    "Stale",  # 本脚本自身
    "_STALE_",
    "stale_keys",  # 清理列表声明 (inject 已删键清单)
    "schema.pop(",
    " in schema",  # 清理逻辑 (pop 已删键)
    "_pf_items",
    "_pfi",  # 清理逻辑变量
    "git push",  # sync_bridge git 操作 (非自研功能)
    "可手动 push",  # sync_bridge 文档文本 (git 操作)
    '"plite_disabled_platforms":',  # i18n 翻译键声明 (上游字段文案)
    'push"',
    "'push'",
    'push"',
    "push'",  # git 命令参数 (非自研功能)
    "--push",
    "args.push",  # sync_bridge CLI 参数
)

SCAN_FILES = [
    *[p for p in (ROOT / "bridge").rglob("*.py") if "tests" not in p.parts],
    ROOT / "main.py",
    ROOT / "run_local.py",
    ROOT / "_conf_schema.json",
    *[p for p in (ROOT / "scripts").rglob("*.py")],
    *[p for p in (ROOT / "tools").rglob("*.py")],
]

# 门禁脚本自身 (STALE_SYMBOLS 声明) 豁免
SELF_NAME = Path(__file__).resolve().name


def main() -> int:
    violations = []
    for f in SCAN_FILES:
        if not f.exists():
            continue
        if f.name == SELF_NAME:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        skip_indent: str | None = (
            None  # 清理列表声明块的缩进 (如 _STALE_CONFIG_KEYS 值行)
        )
        for i, line in enumerate(lines, 1):
            # 清理列表声明块: 变量名含 stale → 其缩进值行豁免
            if "_stale" in line.lower() and "=" in line and "(" in line:
                skip_indent = line[: len(line) - len(line.lstrip())]
                continue
            if skip_indent is not None:
                if line.startswith(skip_indent) and line.strip():
                    if not line.strip().startswith("#"):
                        continue
                skip_indent = None
            for sym in STALE_SYMBOLS:
                if sym not in line:
                    continue
                if any(ctx in line for ctx in ALLOW_CONTEXTS):
                    continue
                violations.append((f.relative_to(ROOT), i, sym, line.strip()))
    if violations:
        print(f"已删符号回潮 {len(violations)} 处:")
        for rel, i, sym, line in violations:
            print(f"  {rel}:{i} [{sym}]  {line[:90]}")
        return 1
    print("OK: 已删自研符号零回潮")
    return 0


if __name__ == "__main__":
    sys.exit(main())
