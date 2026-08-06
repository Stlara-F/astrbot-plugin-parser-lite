"""统一错误处理与发送降级链 (E4).

- safe(): 装饰器 — 捕获异常记日志, 不中断主流程
- send_with_fallback(): 发送降级链 合并转发→拆包单发→纯文本→截断
  所有参数动态获取, 无硬编码阈值由调用方传入.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import functools
import logging
from typing import TypeVar

_logger = logging.getLogger("parser-lite.bridge")

T = TypeVar("T")


def safe(
    logger=None, label: str = ""
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T | None]]]:
    """异步装饰器: 捕获异常, 记录 traceback 摘要, 返回 None 不抛出."""

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _lg = logger or _logger
                _lg.warning(
                    f"[ParserLite] {label or fn.__name__} 失败: {type(exc).__name__}: {exc}"
                )
                return None

        return wrapper

    return deco


async def send_with_fallback(
    *,
    try_send: Callable[[], Awaitable[bool]],
    fallbacks: list[Callable[[], Awaitable[bool]]],
    logger=None,
    label: str = "发送",
) -> bool:
    """发送降级链: 依次尝试 try_send 与 fallbacks, 首个成功即返回.

    :param try_send: 主发送 (如合并转发)
    :param fallbacks: 降级序列 (拆包单发 → 纯文本 → 截断), 每个返回是否成功
    :return: 是否至少一个成功
    """
    _lg = logger or _logger
    attempts = [try_send, *list(fallbacks)]
    for i, fn in enumerate(attempts):
        try:
            ok = await fn()
            if ok:
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _lg.warning(
                f"[ParserLite] {label} 第 {i + 1} 级失败: {type(exc).__name__}: {exc}"
            )
    return False


def truncate_text(text: str, max_len: int) -> str:
    """按长度截断文本 (动态 max_len), 保留末尾省略号."""
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
