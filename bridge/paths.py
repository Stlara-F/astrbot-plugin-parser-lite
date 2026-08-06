"""统一路径解析 (消除多环境路径差异).

规律: 一切目录由单一环境变量 PARSER_LITE_BASE_DIR 驱动:
- 上游 (src/) : base_dir/{cache,config,data}  (config.py 单一来源)
- bridge 状态 : base_dir/parser_lite/*.json (disabled_groups)
- 默认值统一  : cwd/.parser-lite (与上游一致; main.py 在 AstrBot 环境
  setdefault 插件目录/data 保持插件自包含, get_base_dir 读 env 自动生效)
"""

from __future__ import annotations

import os
from pathlib import Path


def get_base_dir() -> Path:
    """解析基础目录: PARSER_LITE_BASE_DIR 优先, 默认 cwd/.parser-lite (与上游一致)."""
    return Path(
        os.environ.get("PARSER_LITE_BASE_DIR") or (Path.cwd() / ".parser-lite")
    ).resolve()


def state_dir() -> Path:
    """bridge 运行时状态目录 (限频/防抖/推送/cookie健康/禁用群组)."""
    return get_base_dir() / "parser_lite"


def ensure_state_dir() -> Path:
    _d = state_dir()
    _d.mkdir(parents=True, exist_ok=True)
    return _d
