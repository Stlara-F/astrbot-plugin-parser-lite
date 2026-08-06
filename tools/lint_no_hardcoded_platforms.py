#!/usr/bin/env python3
"""0 硬编码平台名 lint (REFACTOR_PLAN.md 规范): 生产代码禁止字面量平台名.

豁免:
- src/ (上游源码, 平台名是上游实现的一部分)
- bridge/tests/ (测试断言平台名)
- tools/, scripts/, docs/

用法: python tools/lint_no_hardcoded_platforms.py
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"src", "tools", "scripts", "docs", "test", "tests", ".git", "__pycache__"}

# 平台名字面量 (出现在字符串/注释中即违规)
LITERAL = re.compile(
    r"""["'](bilibili|zhihu|douyin|weibo|xiaohongshu|rednote|tiktok)["']"""
)

# 平台能力声明上下文 (集中声明点, 可审计): 注册表/能力集合初始化
ALLOW_LINES = (
    "_COOKIE_CHECKERS[",
    "_LAZY_DOWNLOAD_PLATFORMS",
    "_COOKIE_SYNC_PLATFORMS",
    "check_bili_cookie,",  # 注册表值引用 (函数定义在 src/ 上游)
    "check_zhihu_cookie,",
    'return {"bilibili"}',  # lazy_download_platforms 回退默认 (声明点)
)


def main() -> int:
    violations = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if not LITERAL.search(line):
                continue
            if any(allowed in line for allowed in ALLOW_LINES):
                continue
            violations.append((rel, i, line.strip()))
    if violations:
        print(f"硬编码平台名 {len(violations)} 处 (平台名应从上游枚举/解析器动态读取):")
        for rel, i, line in violations:
            print(f"  {rel}:{i}  {line}")
        return 1
    print("OK: 无硬编码平台名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
