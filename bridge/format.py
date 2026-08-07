"""纯文本格式化 — 无 AstrBot 依赖, 可独立测试."""

from __future__ import annotations

from nonebot_plugin_parser_lite.data import (
    AudioContent,
    ImageContent,
    ParseResult,
    StickerContent,
    VideoContent,
)


def _safe_label(result: ParseResult) -> str:
    """平台/作者标签 (P3-3: None 防护)."""
    _p = getattr(result, "platform", None)
    _pn = getattr(_p, "display_name", None) or getattr(_p, "name", None) or "解析"
    _a = getattr(result, "author", None)
    _an = getattr(_a, "name", None) or ""
    return f"【{_pn}】{_an}"


def format_full(result: ParseResult) -> str:
    lines = [
        _safe_label(result),
        result.title or "",
    ]
    if result.timestamp:
        lines.append(result.formatted_datetime)
    # 保持 content 原始顺序: 文本 + 贴纸 desc 按序拼接
    texts = []
    for t in result.content:
        if isinstance(t, str):
            texts.append(t)
        elif isinstance(t, StickerContent):
            texts.append(t.desc or "[表情]")
    if texts:
        lines.append("\n" + "\n".join(texts))
    media = []
    for item in result.content:
        if isinstance(item, VideoContent):
            media.append(f"[{item.display_duration}]")
        elif isinstance(item, ImageContent):
            media.append("[图]")
        elif isinstance(item, AudioContent):
            media.append("[音]")
    if media:
        lines.append("\n" + " ".join(media))
    s = result.stats
    stats = []
    if s.view_count:
        stats.append(f"播放{s.view_count}")
    if s.like_count:
        stats.append(f"赞{s.like_count}")
    if s.comment_count:
        stats.append(f"评论{s.comment_count}")
    if s.share_count:
        stats.append(f"分享{s.share_count}")
    if s.collect_count:
        stats.append(f"收藏{s.collect_count}")
    if stats:
        lines.append("\n" + " | ".join(stats))
    if result.comments:
        lines.append(f"\n--- 评论 (共{len(result.comments)}条) ---")
        for i, c in enumerate(result.comments[:5], 1):
            body = " ".join([x for x in c.content if isinstance(x, str)])[:80]
            lines.append(f"[{i}] {c.author.name}: {body}")
    if result.ai_summary and "cookie 未配置" not in result.ai_summary:
        lines.append(f"\nAI摘要: {result.ai_summary[:500]}")
    return "\n".join(lines)


def format_brief(result: ParseResult) -> str:
    lines = [_safe_label(result), result.title or ""]
    s = result.stats
    parts = []
    if s.view_count:
        parts.append(f"播放{s.view_count}")
    if s.like_count:
        parts.append(f"赞{s.like_count}")
    if s.comment_count:
        parts.append(f"评论{s.comment_count}")
    if parts:
        lines.append(" | ".join(parts))
    return "\n".join(lines)
