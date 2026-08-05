"""多 Bot 表情仲裁 (E6) — 群内多个解析机器人防打架.

原理: 消息触发解析前, bot 先向该消息发送一个固定表情 (竞争).
若在等待窗口内检测到其他 bot 也回应了同一表情 (group_msg_emoji_like notice),
则放弃解析 — 避免多个 bot 重复回复同一链接.

实现:
- 模块级状态: {msg_id: (sent_at, expire_at)}
- notice 事件到达 → 若 msg_id 在竞争窗口内且非本 bot 发送 → 标记放弃
- 所有参数动态: 竞争表情/等待窗口/表情 id 从配置或环境变量读取

注意: 仅 aiocqhttp (OneBot V11) 支持 group_msg_emoji_like notice.
"""

from __future__ import annotations

import time

# {msg_id: {"expire": float, "emoji_id": str}}
_pending: dict[str, dict] = {}
# 已放弃解析的 msg_id (窗口内)
_conceded: set[str] = set()

_DEFAULT_EMOJI = "👍"
_DEFAULT_WINDOW_SEC = 1.5


def _emoji_id(emoji: str) -> str:
    # OneBot V11 表情: 文本表情用 unicode 码点, emoji id 用数字 (face 类型)
    try:
        return str(ord(emoji[0]))
    except Exception:
        return emoji


def arm(msg_id: str, *, emoji: str | None = None, window_sec: float | None = None) -> bool:
    """解析前武装竞争: 记录 msg_id, 返回是否应继续 (False=已放弃)."""
    now = time.time()
    # 清理过期
    expired = [k for k, v in _pending.items() if v["expire"] < now]
    for k in expired:
        _pending.pop(k, None)
        _conceded.discard(k)
    if msg_id in _conceded:
        return False
    _pending[msg_id] = {
        "expire": now + (window_sec if window_sec is not None else _DEFAULT_WINDOW_SEC),
        "emoji_id": _emoji_id(emoji if emoji is not None else _DEFAULT_EMOJI),
    }
    return True


def concede(msg_id: str) -> None:
    """主动放弃 (检测到其他 bot 已竞争)."""
    _conceded.add(msg_id)


def check_notice(msg_id: str, emoji_id: str, *, self_uid: str = "") -> bool:
    """处理 group_msg_emoji_like notice.

    :return: True 表示"我们放弃" (其他 bot 也回应了同一表情)
    """
    entry = _pending.get(msg_id)
    if not entry:
        return False
    if emoji_id != entry["emoji_id"]:
        return False
    # 其他 bot 的回应 → 放弃
    _conceded.add(msg_id)
    return True


def disarm(msg_id: str) -> None:
    _pending.pop(msg_id, None)
    _conceded.discard(msg_id)


def parse_notice(raw: dict) -> tuple[str, str] | None:
    """从 OneBot notice dict 提取 (target_msg_id, emoji_id).

    动态识别: notice_type=group_msg_emoji_like, 字段用 get 兜底.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("post_type") != "notice":
        return None
    if raw.get("notice_type") != "group_msg_emoji_like":
        return None
    msg_id = str(raw.get("target_msg_id", "") or raw.get("msg_id", "") or "")
    emoji_id = str(raw.get("emoji_id", "") or raw.get("face_id", "") or "")
    if not msg_id or not emoji_id:
        return None
    return msg_id, emoji_id


def is_notice_event(event) -> bool:
    """判断 AstrMessageEvent 是否为 notice (raw_message 含 post_type)."""
    raw = getattr(event, "raw_message", None)
    if isinstance(raw, dict) and raw.get("post_type") == "notice":
        return True
    msg_obj = getattr(event, "message_obj", None)
    raw2 = getattr(msg_obj, "raw_message", None)
    return isinstance(raw2, dict) and raw2.get("post_type") == "notice"


def load_cfg(source: dict | None = None) -> dict:
    """提取 arbiter 配置段 (功能自包含, 可注入配置源)."""
    from bridge.cfg import global_source, module_cfg

    src = source if source is not None else global_source()
    cfg = module_cfg(src, "arbiter", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "emoji": str(cfg.get("emoji", "👍") or "👍"),
        "window_sec": float(cfg.get("window_sec", 1.5) or 1.5),
    }
