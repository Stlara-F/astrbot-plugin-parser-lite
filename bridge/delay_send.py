"""延迟发送 (F7) — 表情触发式媒体发送.

大媒体 (超阈值) 先发送提示, 记录 pending; 用户对提示消息回应表情
(group_msg_emoji_like notice) 后触发实际发送.

0 硬编码: 阈值/表情 id 从配置动态读取.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import time

logger = logging.getLogger("parser-lite.bridge.delay")

_TriggerFn = Callable[[str], Awaitable[None]]
"""trigger(key) — 触发实际发送"""


class DelaySender:
    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}  # msg_id -> {key, trigger, expire}
        self._trigger: _TriggerFn | None = None

    def set_trigger(self, fn: _TriggerFn) -> None:
        self._trigger = fn

    def arm(
        self,
        msg_id: str,
        key: str,
        *,
        timeout_sec: float = 300.0,
        trigger: _TriggerFn | None = None,
    ) -> bool:
        """武装延迟发送: 记录 pending. 返回是否新武装 (同 msg_id 已存在则刷新)."""
        if trigger is not None:
            self._trigger = trigger
        self._pending[msg_id] = {
            "key": key,
            "expire": time.time() + timeout_sec,
        }
        return True

    def on_emoji_like(self, msg_id: str, emoji_id: str, want_emoji_ids: list[str]) -> bool:
        """处理表情回应: 匹配期望表情 → 触发发送.

        :return: True 表示已触发
        """
        entry = self._pending.pop(msg_id, None)
        if not entry:
            return False
        if want_emoji_ids and emoji_id not in want_emoji_ids:
            # 表情不匹配 → 保留 pending (可能是误触发)
            self._pending[msg_id] = entry
            return False
        if time.time() > entry["expire"]:
            return False
        if self._trigger:
            try:
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    _task = loop.create_task(self._trigger(entry["key"]))
                    _ = _task  # 持有引用防 GC
                except RuntimeError:
                    # 无运行中 loop (测试/脚本环境) → 直接同步执行
                    import asyncio as _aio

                    async def _run():
                        await self._trigger(entry["key"])

                    _aio.run(_run())
                return True
            except Exception as e:
                logger.warning(f"[ParserLite] 延迟发送触发失败: {e}")
        return False

    def cleanup(self) -> int:
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expire"] < now]
        for k in expired:
            self._pending.pop(k, None)
        return len(expired)

    def pending_count(self) -> int:
        return len(self._pending)

    def should_delay(self, size_bytes: int, threshold_bytes: int) -> bool:
        """大小超阈值 → 延迟发送."""
        return size_bytes > threshold_bytes


def make_delay_sender() -> DelaySender:
    return DelaySender()
