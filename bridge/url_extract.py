"""全消息类型 URL 抽取管线 — 0 hardcode, 无 AstrBot event 依赖.

Comp 类型以参数注入 (isinstance 判断用), 便于脱离 AstrBot 环境测试.
"""

from __future__ import annotations

import json
import re

URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)


def url_from_text(get_message_str) -> str | None:
    """从消息文本提取首个 URL."""
    m = URL_RE.search(str(get_message_str()).strip())
    return m.group(0) if m else None


def collect_urls(text: str, urls: list[str]) -> None:
    for m in URL_RE.finditer(text):
        urls.append(m.group(0))


def extract_card_json_url(data) -> str | None:
    """从 Json 组件 .data 中动态提取 URL (0 hardcode: 递归扫描所有含 url/link 键的值)"""
    try:
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    # BFS 递归扫描: 优先匹配命名键 (含 url/link 字样), 兜底全文字符串
    queue: list[tuple] = [(data, "")]
    named_urls: list[str] = []
    raw_urls: list[str] = []

    while queue:
        obj, pkey = queue.pop(0)
        if isinstance(obj, dict):
            for k, v in obj.items():
                queue.append((v, str(k).lower()))
        elif isinstance(obj, list):
            for item in obj:
                queue.append((item, pkey))
        elif isinstance(obj, str) and len(obj) > 10:
            if "url" in pkey or "link" in pkey:
                if obj.startswith("http"):
                    named_urls.append(obj)
            elif URL_RE.match(obj):
                if not any(x in obj.lower() for x in ("icon", "logo", "avatar", "thumbnail", "imageview", ".png", ".jpg", ".ico")):
                    raw_urls.append(obj)

    return (named_urls[0] if named_urls else
            raw_urls[0] if raw_urls else None)


def extract_json_urls(raw: str | dict, urls: list[str]) -> None:
    """从 JSON 卡片 data 中提取嵌入 URL (递归 BFS)"""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    queue = [payload]
    keys = ("url", "jumpUrl", "qqdocurl", "share_url", "jump_url", "link", "action_url",
            "source_url", "redirect_url", "preview_url", "article_url")
    seen_objs = set()
    while queue:
        obj = queue.pop(0)
        if id(obj) in seen_objs:
            continue
        seen_objs.add(id(obj))
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k, "")
                if isinstance(v, str) and URL_RE.match(v):
                    urls.append(v)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(obj, list):
            queue.extend(obj)


def extract_xml_urls(raw: str, urls: list[str]) -> None:
    """从 XML 卡片中提取 URL"""
    for tag in ("url", "qqdocurl", "jumpUrl", "share_url", "link"):
        for m in re.finditer(rf"""<{tag}>\s*(https?://[^<\s]+)\s*</{tag}>""", raw, re.IGNORECASE):
            urls.append(m.group(1).strip())
    for attr in ("url", "qqdocurl", "jumpUrl", "share_url"):
        for m in re.finditer(rf"""{attr}\s*=\s*['"](https?://[^\s'"<>]+)['"]""", raw, re.IGNORECASE):
            urls.append(m.group(1).strip())


def extract_forward_urls(seg_data, urls: list[str]) -> None:
    """从合并转发节点中递归提取 URL"""
    if isinstance(seg_data, dict):
        msgs = seg_data.get("messages", []) or seg_data.get("content", [])
    elif isinstance(seg_data, str):
        try:
            parsed = json.loads(seg_data)
            msgs = parsed.get("messages", []) if isinstance(parsed, dict) else []
        except (json.JSONDecodeError, TypeError):
            return
    else:
        msgs = seg_data if isinstance(seg_data, list) else []
    if not isinstance(msgs, list):
        return
    for node in msgs:
        if not isinstance(node, dict):
            continue
        sub_msgs = node.get("message", None) or node.get("content", None)
        if isinstance(sub_msgs, list):
            for sub in sub_msgs:
                if isinstance(sub, dict):
                    sub_data = sub.get("data", {})
                    if isinstance(sub_data, dict):
                        for f in ("text", "content", "url"):
                            v = sub_data.get(f, "")
                            if v:
                                collect_urls(str(v), urls)
            for sub in sub_msgs:
                if isinstance(sub, dict) and sub.get("type") in ("forward", "node"):
                    extract_forward_urls(sub.get("data", {}), urls)
        elif isinstance(sub_msgs, str):
            collect_urls(sub_msgs, urls)


def extract_urls(event, comp_cls) -> list[str]:
    """从 AstrBot 事件中提取全部 URL (Comp 类以参数传入).

    :param event: AstrBot AstrMessageEvent
    :param comp_cls: astrbot.api.message_components 模块 (isinstance 判断)
    """
    urls: list[str] = []

    # 1. 遍历 AstrBot 消息链
    try:
        chain = event.get_messages()
    except Exception:
        chain = []
    if not chain:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            chain = getattr(msg_obj, "message", None) or getattr(msg_obj, "message_chain", None) or []
    if not isinstance(chain, list):
        chain = []

    for seg in chain:
        if isinstance(seg, comp_cls.Json):
            url = extract_card_json_url(seg.data)
            if url:
                urls.append(url)
        elif isinstance(seg, comp_cls.Image):
            pass  # 图片不含 URL
        else:
            seg_data = None
            seg_type = ""
            if isinstance(seg, dict):
                seg_type = str(seg.get("type", "")).lower()
                seg_data = seg.get("data", {}) or {}
            elif hasattr(seg, "type"):
                seg_type = str(getattr(seg, "type", "")).lower()
                seg_data = getattr(seg, "data", None)

            if seg_type == "text" or "text" in seg_type:
                t = seg_data.get("text", "") if isinstance(seg_data, dict) else str(seg_data or "")
                collect_urls(t, urls)
            elif "json" in seg_type or "miniapp" in seg_type:
                d = seg_data.get("data", "") if isinstance(seg_data, dict) else str(seg_data or "")
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                if isinstance(d, str) and d:
                    url = extract_card_json_url(d)
                    if url:
                        urls.append(url)
                    collect_urls(d, urls)
            elif "xml" in seg_type:
                d = seg_data.get("data", "") if isinstance(seg_data, dict) else str(seg_data or "")
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                extract_xml_urls(d, urls)
            elif "reply" in seg_type:
                if isinstance(seg_data, dict):
                    rt = seg_data.get("text", "") or seg_data.get("message", "") or ""
                    if isinstance(rt, list):
                        rt = " ".join((s.get("data", {}).get("text", "") if isinstance(s, dict) else str(s)) for s in rt)
                    collect_urls(str(rt), urls)
            elif "markdown" in seg_type:
                d = seg_data.get("data", "") or seg_data.get("content", "")
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                collect_urls(str(d or ""), urls)
            elif "forward" in seg_type:
                extract_forward_urls(seg_data or {}, urls)

    # 2. 纯文本兜底
    text = event.get_message_str()
    if text:
        collect_urls(text, urls)

    # 3. 去重 + 清理尾部标点
    seen = set()
    result = []
    for u in urls:
        u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result
