"""渲染适配 (bridge-refactor: 对齐上游 render/ 原生实现).

上游 render_html 自建 env (safe_src 注册 + autoescape), 模板省略 method
参数 ({% set src = cont | safe_src %}) → 仅需包装 safe_src 给默认 method.
send_card: 上游 RENDERER.render_image → AstrBot 发送 + LRU 缓存 + 文本回退.
"""

from __future__ import annotations

import functools
from typing import Any

# 原方法引用 (restore_render_patch 可还原)
_ORIGINALS: dict[str, Any] = {}


def apply_render_patch() -> bool:
    """上游渲染缺陷修复 (最小集): safe_src 缺 method 默认值.

    可还原: 原方法引用保存在 _ORIGINALS, restore_render_patch() 恢复.
    :return: 是否已应用
    """
    try:
        import nonebot_plugin_parser_lite.render as _render

        if getattr(_render.safe_src, "_pl_default_method", False):
            return True  # 已 patch

        _orig_safe_src = _render.safe_src
        _ORIGINALS["safe_src"] = _orig_safe_src

        @functools.wraps(_orig_safe_src)
        async def _patched_safe_src(obj: Any, method: str = "get_path", **kw):
            return await _orig_safe_src(obj, method, **kw)

        _patched_safe_src._pl_default_method = True  # type: ignore[attr-defined]
        _render.safe_src = _patched_safe_src
        return True
    except Exception:
        return False


def restore_render_patch() -> bool:
    """还原渲染补丁 (恢复原方法引用)."""
    try:
        import nonebot_plugin_parser_lite.render as _render

        if "safe_src" in _ORIGINALS:
            _render.safe_src = _ORIGINALS.pop("safe_src")
        return True
    except Exception:
        return False


# ── 卡片发送 (上游渲染 → AstrBot 发送 + 缓存 + 文本回退) ────────────────────

_CARD_CACHE_MAX = 10
_CARD_CACHE: dict[str, bytes] = {}


def _image_from_bytes(data: bytes):
    """AstrBot 图片组件 (延迟 import, CI 无 astrbot 时可导入本模块)."""
    from astrbot.api.message_components import Image

    return Image.fromBytes(data)


def _plain(text: str):
    from astrbot.api.message_components import Plain

    return Plain(text)


async def send_card(event, result, format_full, logger=None):
    """渲染卡片并发送 (上游渲染 → AstrBot 发送); 失败回退文本.

    :return: SendReport (bool(report)=ok, 兼容旧调用)
    """
    from bridge.send import SendReport, _log_onebot11, _onebot11_segments

    _report = SendReport(stage="card")
    if logger is None:
        import logging

        logger = logging.getLogger("parser-lite.bridge.render")

    cache_key = result.url
    if cache_key in _CARD_CACHE:
        data = _CARD_CACHE.pop(cache_key)
        _CARD_CACHE[cache_key] = data
        _segs = [_image_from_bytes(data)]
        _log_onebot11(logger, "card-cache", _segs)
        await event.send(event.chain_result(_segs))
        logger.info(f"[ParserLite] card cache hit ({len(data)} bytes)")
        _report.ok, _report.stage = True, "card-cache"
        _report.segments = _onebot11_segments(_segs)
        return _report

    try:
        from bridge.context import up_renderer

        data = await up_renderer().render_image(result)
        if len(data) < 1024 or data[:2] != b"\xff\xd8":
            raise RuntimeError(f"Invalid JPEG: {len(data)} bytes")
        if len(_CARD_CACHE) >= _CARD_CACHE_MAX:
            _CARD_CACHE.pop(next(iter(_CARD_CACHE)), None)
        _CARD_CACHE[cache_key] = data
        _segs = [_image_from_bytes(data)]
        _log_onebot11(logger, "card", _segs)
        await event.send(event.chain_result(_segs))
        logger.info(f"[ParserLite] card rendered ({len(data)} bytes)")
        _report.ok, _report.stage = True, "card"
        _report.segments = _onebot11_segments(_segs)
        return _report
    except Exception as _e:
        _reason = f"{type(_e).__name__}: {_e}"
        logger.warning(f"[ParserLite] 卡片渲染失败, 回退文本 ({_reason})")
        _report.errors.append(f"render: {_reason}")
        try:
            _segs = [_plain(format_full(result))]
            _log_onebot11(logger, "card-fallback", _segs)
            await event.send(event.chain_result(_segs))
            _report.ok, _report.stage = True, "card-fallback"
            _report.segments = _onebot11_segments(_segs)
            return _report
        except Exception as _e2:
            _reason2 = f"{type(_e2).__name__}: {_e2}"
            logger.error(
                f"[ParserLite] 回退文本发送也失败 (OneBot API 可能不可用): {_reason2}"
            )
            _report.stage = "card-fallback"
            _report.errors.append(f"send: {_reason2}")
            return _report
