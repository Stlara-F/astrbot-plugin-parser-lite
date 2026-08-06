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

# 持有触发 task 引用 (防 GC; 完成即由 done_callback 释放)
_PENDING_TRIGGERS: set = set()

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

                # P2-3: 生产环境始终存在运行中 loop; 无 loop (脚本/测试) 不触发
                # (同步执行分支已删除 — 死代码)
                loop = asyncio.get_running_loop()
                _t = loop.create_task(self._trigger(entry["key"]))
                _PENDING_TRIGGERS.add(_t)  # 持有引用防 GC, 完成即释放
                _t.add_done_callback(_PENDING_TRIGGERS.discard)
                return True
            except RuntimeError:
                logger.debug("[ParserLite] 延迟发送: 无运行中事件循环, 跳过触发")
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


def load_cfg(source: dict | None = None) -> dict:
    """提取 delay_send 配置段 (功能自包含, 可注入配置源).

    :param source: 配置源 (None → 全局 BridgeConfig)
    :return: {"enabled", "threshold_mb", "timeout_sec", "emoji_ids"}
    """
    from bridge.cfg import global_source, module_cfg

    src = source if source is not None else global_source()
    cfg = module_cfg(src, "delay_send", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "threshold_mb": int(cfg.get("threshold_mb", 20) or 20),
        "timeout_sec": float(cfg.get("timeout_sec", 300) or 300),
        "emoji_ids": cfg.get("emoji_ids", ["128077"]),
    }
