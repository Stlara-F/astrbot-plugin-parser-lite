#!/usr/bin/env python3
"""重建 bridge-standalone: 以最新上游 standalone 为基底, 叠加 bridge 功能层.

背景: 上游 standalone 是"快照模型" (每次基于最新 main 重新生成, 无持续历史),
bridge 无法 rebase 跟随. 本脚本从旧 bridge 提交树提取功能文件, 叠加到新基底.

用法:
  python scripts/sync_bridge.py [--ref <旧bridge提交>] [--push]

流程:
  1. fetch upstream standalone
  2. 若 bridge-standalone 落后上游 → 用 git 树提取 bridge 功能文件
  3. checkout 新基底 → 恢复文件 → commit → (可选推送到 origin)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# bridge 功能路径 (相对仓库根) — 从旧提交树提取
BRIDGE_PATHS = [
    "bridge",
    "main.py",
    "run_local.py",
    "pytest.ini",
    "ruff.toml",
    "test",
    "GUIDE.md",
    "metadata.yaml",
    ".github/workflows/linting.yml",
    ".github/workflows/ruff.yml",
]


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败: {' '.join(args)}")
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ref",
        default="origin/bridge-standalone",
        help="旧 bridge 提交引用 (默认 origin/bridge-standalone)",
    )
    ap.add_argument("--push", action="store_true", help="推送 origin")
    args = ap.parse_args()

    sh("git", "fetch", "upstream", "standalone")
    sh("git", "fetch", "origin", "bridge-standalone")

    upstream_sha = sh("git", "rev-parse", "upstream/standalone")
    behind = sh("git", "rev-list", "--count", f"{args.ref}..upstream/standalone")
    if behind == "0":
        print("bridge 已包含最新上游 standalone, 跳过")
        return 0
    print(f"bridge 落后上游 {behind} 提交 → 重建")

    # 从旧提交树提取功能文件 (git 树提取, 避免工作区产物)
    for p in BRIDGE_PATHS:
        if sh("git", "cat-file", "-e", f"{args.ref}:{p}", check=False):
            sh("git", "checkout", f"{args.ref}", "--", p)

    # 新基底
    sh("git", "checkout", "-B", "bridge-standalone", "upstream/standalone")
    # 恢复功能文件
    for p in BRIDGE_PATHS:
        if sh("git", "cat-file", "-e", f"{args.ref}:{p}", check=False):
            sh("git", "checkout", f"{args.ref}", "--", p)

    sh("git", "add", "-A")
    if sh("git", "status", "--porcelain"):
        sh(
            "git",
            "commit",
            "-m",
            f"bridge: rebase onto upstream standalone ({upstream_sha[:12]})",
        )
        print(f"已重建 bridge-standalone @ {upstream_sha[:12]}")
    else:
        print("无变更")

    if args.push:
        sh("git", "push", "origin", "bridge-standalone", "--force-with-lease")
        print("已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
