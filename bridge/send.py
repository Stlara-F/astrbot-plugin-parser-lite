"""发送层: ParseResult → AstrBot 消息 (扩展层, 与上游完全解耦).

原始调用保留: RENDERER.render_image(result) (上游 Playwright 渲染)
扩展逻辑: AstrBot Comp.Image/Plain 发送 + LRU 缓存 + 文本回退.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from bridge.cfg import read_cfg
from bridge.context import BridgeConfig, up_renderer

_CARD_CACHE_MAX = 10
_CARD_CACHE: OrderedDict[str, bytes] = OrderedDict()


def get_sendable_types() -> list[str]:
    """动态扫描上游 ContentItem Union 成员 → 可发送类型列表 (0 hardcode)."""
    from nonebot_plugin_parser_lite.data import ContentItem

    mapping = {}
    for cls in getattr(ContentItem, "__args__", []) or []:
        name = getattr(cls, "__name__", "")
        if name in ("ImageContent", "VideoContent", "AudioContent"):
            mapping[name] = {"ImageContent": "image", "VideoContent": "video",
                             "AudioContent": "audio"}[name]
    return list(dict.fromkeys(["card", *mapping.values()]))


def should_send(media_type: str) -> bool:
    """发送策略门 (配置驱动, 默认全类型)."""
    try:
        s = read_cfg(BridgeConfig._source or {}, "send_strategy", get_sendable_types())
        if isinstance(s, str):
            import json
            try:
                s = json.loads(s)
            except Exception:
                s = get_sendable_types()
        return media_type in (s if isinstance(s, list) else [])
    except Exception:
        return True


async def send_card(event, result, format_full: Callable, logger=None) -> bool:
    """渲染卡片并发送 (上游渲染 → AstrBot 发送); 失败回退文本.

    :return: 是否成功
    """
    if logger is None:
        import logging
        logger = logging.getLogger("parser-lite.bridge.send")

    cache_key = result.url
    if cache_key in _CARD_CACHE:
        data = _CARD_CACHE.pop(cache_key)
        _CARD_CACHE[cache_key] = data
        await event.send(event.chain_result([_image_from_bytes(data)]))
        logger.info(f"[ParserLite] card cache hit ({len(data)} bytes)")
        return True

    try:
        data = await up_renderer().render_image(result)
        if len(data) < 1024 or data[:2] != b"\xff\xd8":
            raise RuntimeError(f"Invalid JPEG: {len(data)} bytes")
        if len(_CARD_CACHE) >= _CARD_CACHE_MAX:
            _CARD_CACHE.pop(next(iter(_CARD_CACHE)), None)
        _CARD_CACHE[cache_key] = data
        await event.send(event.chain_result([_image_from_bytes(data)]))
        logger.info(f"[ParserLite] card rendered ({len(data)} bytes)")
        return True
    except Exception:
        logger.warning("[ParserLite] 卡片渲染失败, 回退文本")
        try:
            await event.send(event.chain_result([_plain(format_full(result))]))
            return True
        except Exception:
            logger.error("[ParserLite] 回退文本发送也失败 (OneBot API 可能不可用)")
            return False


def _image_from_bytes(data: bytes):
    """AstrBot 图片组件 (延迟 import, CI 无 astrbot 时可导入本模块)."""
    from astrbot.api.message_components import Image

    return Image.fromBytes(data)


def _plain(text: str):
    from astrbot.api.message_components import Plain

    return Plain(text)


async def send_media_file(event, path, media_type: str, source_url: str = "",
                          converters: dict | None = None, logger=None) -> bool:
    """媒体三路发送骨架 (fromFileSystem → bytes/base64 → fromURL).

    :param converters: 扩展回调 {"image": async fn(path)->bytes, "video": async fn(path)->Path,
                       "audio": async fn(path)->Path} — 由调用方注入 (FFmpeg 转换保持扩展)
    :return: 是否成功发送
    """
    import base64
    from pathlib import Path

    if logger is None:
        import logging
        logger = logging.getLogger("parser-lite.bridge.send")

    p = Path(path)
    if not p.exists():
        logger.warning(f"[ParserLite] send_media_file: file missing {p}")
        return False
    try:
        p.chmod(0o644)
    except Exception:
        pass
    converters = converters or {}
    # 组件延迟 import (CI/离线无 astrbot 时可导入模块, 发送时才需组件)
    from astrbot.api.message_components import Image, Record, Video

    if media_type == "image":
        try:
            await event.send(event.chain_result([Image.fromFileSystem(str(p))]))
            return True
        except Exception:
            pass
        try:
            raw = p.read_bytes()
            compress = converters.get("image")
            if compress is not None:
                raw = await compress(p)
            await event.send(event.chain_result([Image.fromBytes(raw)]))
            return True
        except Exception:
            pass
        if source_url:
            try:
                await event.send(event.chain_result([Image.fromURL(source_url)]))
                return True
            except Exception:
                pass

    elif media_type == "video":
        conv = converters.get("video")
        mp4 = await conv(p) if conv else p
        mp4 = Path(mp4)
        try:
            await event.send(event.chain_result([Video.fromFileSystem(str(mp4))]))
            return True
        except Exception:
            pass
        try:
            raw = mp4.read_bytes()
            b64 = base64.b64encode(raw).decode()
            await event.send(event.chain_result([Video.fromBase64(b64)]))
            return True
        except Exception:
            pass
        if source_url:
            try:
                await event.send(event.chain_result([Video.fromURL(source_url)]))
                return True
            except Exception:
                pass

    elif media_type == "audio":
        conv = converters.get("audio")
        mp3 = await conv(p) if conv else p
        mp3 = Path(mp3)
        try:
            await event.send(event.chain_result([Record.fromFileSystem(str(mp3))]))
            return True
        except Exception:
            pass
        try:
            raw = mp3.read_bytes()
            await event.send(event.chain_result([Record.fromBytes(raw)]))
            return True
        except Exception:
            pass
        if source_url:
            try:
                await event.send(event.chain_result([Record.fromURL(source_url)]))
                return True
            except Exception:
                pass
    return False
