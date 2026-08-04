"""提交前校验测试 — schema 注入顺序 + 无硬编码 (CI commit gate).

调用 scripts/check_schema.py, 验证:
- _BRIDGE_FIELDS 使用频率排序 (高频在前)
- 无重复配置路径
- 必含新增配置项
- 嵌套 parsers.items 顺序
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent


def test_check_schema_passes():
    """运行提交前校验脚本, 必须退出码 0."""
    r = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "check_schema.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"check_schema 失败:\n{r.stdout}\n{r.stderr}"
    assert "OK" in r.stdout
