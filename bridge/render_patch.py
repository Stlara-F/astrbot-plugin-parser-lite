"""上游 standalone render 兼容补丁 (0 侵入).

上游 bug: render.py.tmpl 的 safe_src 要求 method 参数,
但模板多处省略 ({% set src = cont | safe_src %} / safe_src(return_none_on_fail=True)).
→ 包装 safe_src 给 method 默认值 "get_path".
"""

from __future__ import annotations

import functools
from typing import Any


def apply_render_patch() -> bool:
    """给 nonebot_plugin_parser_lite.render.safe_src 加默认 method.

    :return: 是否已应用
    """
    try:
        import nonebot_plugin_parser_lite.render as _render
        if getattr(_render.safe_src, "_pl_default_method", False):
            return True  # 已 patch

        _orig = _render.safe_src

        @functools.wraps(_orig)
        async def _patched(obj: Any, method: str = "get_path", **kw):
            return await _orig(obj, method, **kw)

        _patched._pl_default_method = True  # type: ignore[attr-defined]
        _render.safe_src = _patched
        return True
    except Exception:
        return False
