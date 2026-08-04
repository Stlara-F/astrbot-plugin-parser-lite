"""QQ 卡片 → LLM 结构化文本注入 (F2) — 独立模块.

将 OneBot Json 卡片组件转换为结构化文本 ([分享]标题/描述/来源/链接),
注入 event.message_str 供 LLM 理解; 增强 reply 引用链.

0 硬编码: 字段路径用优先清单 get 兜底, 卡片类型用 app/prompt 前缀动态识别.
"""

from __future__ import annotations

import json
from typing import Any


def _pick_first(obj: dict, paths: list[tuple[str, ...]]) -> str:
    """按路径优先清单取值 (0 硬编码: 多个可能字段兜底)."""
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur:
            return cur
    return ""


def parse_miniapp_card(data: dict) -> dict | None:
    """小程序卡片: com.tencent.miniapp."""
    meta = data.get("meta", {}) or {}
    detail = (meta.get("detail_1") or {}) or (meta.get("news") or {})
    title = _pick_first(detail, [("title",), ("desc",), ("name",)])
    desc = _pick_first(detail, [("desc",), ("summary",)])
    url = _pick_first(detail, [
        ("qqdocurl",), ("jumpUrl",), ("url",), ("preview_url",),
    ])
    source = _pick_first(detail, [("source",), ("tag",), ("app",)])
    if not title and not url:
        return None
    return {"kind": "小程序", "title": title, "desc": desc, "source": source, "url": url}


def parse_link_share_card(data: dict) -> dict | None:
    """链接分享卡片: com.tencent.structmsg view=news."""
    meta = data.get("meta", {}) or {}
    news = meta.get("news", {}) or {}
    title = _pick_first(news, [("title",), ("desc",)])
    desc = _pick_first(news, [("desc",), ("preview",)])
    url = _pick_first(news, [("qqdocurl",), ("jumpUrl",), ("url",)])
    source = _pick_first(news, [("source",), ("tag",)])
    if not title and not url:
        return None
    return {"kind": "链接分享", "title": title, "desc": desc, "source": source, "url": url}


def parse_music_card(data: dict) -> dict | None:
    """音乐卡片: com.tencent.music view=music."""
    meta = data.get("meta", {}) or {}
    music = meta.get("music", {}) or {}
    title = _pick_first(music, [("title",), ("desc",)])
    desc = _pick_first(music, [("desc",), ("summary",)])
    url = _pick_first(music, [("jumpUrl",), ("url",), ("preview_url",)])
    source = _pick_first(music, [("source",), ("tag",)])
    if not title and not url:
        return None
    return {"kind": "音乐", "title": title, "desc": desc, "source": source, "url": url}


def parse_card(data: Any) -> dict | None:
    """入口: 解析任意 Json 卡片数据 (dict 或 JSON 字符串)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    app = str(data.get("app", "")).lower()
    prompt = str(data.get("prompt", "")).lower()
    # 动态识别卡片类型
    if "miniapp" in app:
        return parse_miniapp_card(data)
    if "music" in app or ("view" in prompt and "music" in str(data.get("view", "")).lower()):
        return parse_music_card(data)
    if "structmsg" in app or "news" in str(data.get("view", "")).lower():
        return parse_link_share_card(data)
    # 兜底: 通用提取
    meta = data.get("meta", {}) or {}
    if isinstance(meta, dict) and meta:
        return parse_link_share_card(data) or parse_miniapp_card(data)
    return None


def format_card_summary(card: dict) -> str:
    """结构化输出."""
    lines = ["[分享]"]
    if card.get("title"):
        lines.append(f"标题: {card['title']}")
    if card.get("desc"):
        lines.append(f"描述: {str(card['desc'])[:100]}")
    if card.get("source"):
        lines.append(f"来源: {card['source']}")
    if card.get("url"):
        lines.append(f"链接: {card['url']}")
    return "\n".join(lines)


def find_json_cards(event) -> list[dict]:
    """从 AstrBot 事件消息链中提取所有 Json 卡片, 返回 (data, card) 列表.

    0 硬编码: 用 isinstance 判断 Json 组件, dict 段用 type 判断.
    """
    results: list[dict] = []
    chain = None
    try:
        chain = event.get_messages()
    except Exception:
        pass
    if not chain:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            chain = getattr(msg_obj, "message", None) or []
    for seg in chain if isinstance(chain, list) else []:
        data = None
        if hasattr(seg, "data"):  # AstrBot Comp.Json
            data = getattr(seg, "data", None)
        elif isinstance(seg, dict) and "json" in str(seg.get("type", "")).lower():
            data = seg.get("data", "")
        if data:
            card = parse_card(data)
            if card:
                results.append({"raw": data, "card": card})
    return results


def inject_card_summary(event, card: dict) -> None:
    """将卡片摘要注入 event.message_str (LLM 可读), 并增强 reply 链."""
    summary = format_card_summary(card)
    try:
        current = event.get_message_str() or ""
        if summary not in current:
            event.message_str = f"{summary}\n{current}" if current else summary
    except Exception:
        pass
    msg_obj = getattr(event, "message_obj", None)
    if msg_obj and not hasattr(msg_obj, "message_str"):
        try:
            msg_obj.message_str = summary
        except Exception:
            pass
