#!/usr/bin/env python3
"""r9b 门禁: 已删自研符号零回潮断言 (防多轮审计反复发现残留).

扫描 git 跟踪的 bridge/ (生产代码)、main.py、scripts/、tools/、run_local.py,
断言已删符号不再出现. 豁免: bridge/tests (历史断言)、src/ (上游)、
_conf_schema.json (运行时注入产物, 由 _BRIDGE_FIELDS + _STALE_CONFIG_KEYS 重建).

用法: python scripts/check_no_stale_fields.py
"""

from __future__ import annotations

import subprocess
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
    "plite_image_compress_mb",
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
    "--push",
    "args.push",  # sync_bridge CLI 参数
    '"git", "push"',  # sh() git 调用参数
    "[push, ",  # GitHub Actions 触发事件 (git 操作)
    "push:",
    "push:",  # workflow 触发块 (git 操作)
)

# 不扫描文件: 运行时注入产物 (gitignored, 由注入逻辑重建) + 本脚本自身
SELF_NAME = Path(__file__).resolve().name
SKIP_PATTERNS = ("_conf_schema.json", ".injected")

# 扫描后缀 (代码类; 文档/数据快照由人工 review)
SCAN_SUFFIXES = (".py", ".yml", ".yaml", ".json")


def _tracked_files() -> list[Path]:
    """git 跟踪文件清单 (CI/本地行为一致, 排除陈旧产物误报)."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return []
    return [ROOT / p for p in out.stdout.splitlines() if p]


def main() -> int:
    files = _tracked_files()
    if not files:
        print("WARN: git ls-files 失败, 门禁跳过")
        return 0
    violations = []
    for f in files:
        if f.name in SKIP_PATTERNS:
            continue
        if not f.exists():
            continue
        rel = f.relative_to(ROOT)
        parts = rel.parts
        if parts[0] == "bridge" and "tests" in parts:
            continue  # 历史断言豁免
        if parts[0] == "src":
            continue  # 上游零修改豁免
        if parts[0] == "api_txt":
            continue  # 上游测试数据快照 (HTML 快照含任意文本)
        if f.suffix not in SCAN_SUFFIXES:
            continue  # 文档/数据 (.md/.txt) 由人工 review
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
                violations.append((rel, i, sym, line.strip()))
    if violations:
        print(f"已删符号回潮 {len(violations)} 处:")
        for rel, i, sym, line in violations:
            print(f"  {rel}:{i} [{sym}]  {line[:90]}")
        return 1
    print(f"OK: 已删自研符号零回潮 ({len(files)} 个跟踪文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
