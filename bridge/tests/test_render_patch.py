"""渲染适配测试 (bridge-refactor: 对齐上游 render/ 原生实现).

- safe_src 缺 method 调用兼容 (上游模板省略 method 参数)
- up_renderer 引用对齐 (main.py 清 sys.modules 后缓存跟随)
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


@pytest.mark.asyncio
async def test_safe_src_no_method():
    """模板省略 method → 默认 get_path (上游模板 {% set src = cont | safe_src %})."""
    from bridge.render import apply_render_patch
    from nonebot_plugin_parser_lite.render import safe_src

    apply_render_patch()
    # 调用省略 method (patch 提供默认值)
    out = await safe_src(None, return_none_on_fail=True)
    assert out is None  # obj=None → 返回 None 而非 TypeError


@pytest.mark.asyncio
async def test_safe_src_explicit_method():
    from bridge.render import apply_render_patch
    from nonebot_plugin_parser_lite.render import safe_src

    apply_render_patch()
    out = await safe_src(None, "get_path", return_none_on_fail=True)
    assert out is None


@pytest.mark.asyncio
async def test_render_patch_idempotent():
    from bridge.render import apply_render_patch, restore_render_patch

    apply_render_patch()
    assert apply_render_patch() is True  # 幂等
    restore_render_patch()
    assert apply_render_patch() is True  # 还原后可重打


def test_up_renderer_refresh_after_upstream_rebuild():
    """上游模块被 main.py 清理重建后, up_renderer() 必须对齐最新模块.

    回归: 缓存 _UP_RENDERER 指向旧实例, patch 作用于新模块 → 调用漂移.
    """
    from bridge import adapter
    from nonebot_plugin_parser_lite import render as render_mod

    # 触发首次缓存 + patch
    _r1 = adapter.up_renderer()
    assert getattr(render_mod.safe_src, "_pl_default_method", False)

    # 模拟 main.py 清 sys.modules → 上游模块重建
    for _m in list(sys.modules):
        if _m.startswith("nonebot_plugin_parser_lite"):
            del sys.modules[_m]

    _r2 = adapter.up_renderer()
    assert _r2 is not _r1  # 已重建
    from nonebot_plugin_parser_lite import render as render_mod2

    assert getattr(render_mod2.safe_src, "_pl_default_method", False)  # 新模块已 patch
