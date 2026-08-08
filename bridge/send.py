"""发送层: ParseResult → AstrBot 消息 (扩展层, 与上游完全解耦).

原始调用保留: RENDERER.render_image(result) (上游 Playwright 渲染)
扩展逻辑: AstrBot Comp 发送 + 多级 failback + OneBot11 段序列化.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import functools
import logging
from pathlib import Path
from typing import TypeVar

from bridge.config import bridge_cfg
from nonebot_plugin_parser_lite.data import (  # noqa: E402
    AudioContent,
    ImageContent,
    ParseResult,
    StickerContent,
    VideoContent,
)


@dataclass
class SendReport:
    """发送结果报告 (bool(report) = report.ok, 兼容旧调用).

    并发安全: 每次调用返回独立实例, 不再共享全局可变状态.
    """

    ok: bool = False
    stage: str = ""
    segments: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def snapshot(self) -> dict:
        """模块级诊断快照 (并发下仅反映最近一次完成的任务)."""
        return {
            "ok": self.ok,
            "segments": self.segments,
            "errors": self.errors,
            "stage": self.stage,
        }


_COMPONENTS_CACHE: dict | None = None


def _get_components():
    """延迟获取 AstrBot 组件 (缓存, 避免重复 import 触发 AstrBot
    sqlalchemy 表重复定义; CI/测试可注入假组件)."""
    global _COMPONENTS_CACHE
    if _COMPONENTS_CACHE is None:
        from astrbot.api.message_components import File, Image, Record, Video

        _COMPONENTS_CACHE = {
            "File": File,
            "Image": Image,
            "Record": Record,
            "Video": Video,
        }
    return _COMPONENTS_CACHE


def _onebot11_segments(segments) -> list[dict]:
    """将 AstrBot 组件段序列化为 OneBot11 数组格式 (type/data, 值均为字符串).

    参考 OneBot11 消息段数组: [{"type": "image", "data": {"file": "..."}}]
    """
    out = []
    for s in segments or []:
        t = getattr(s, "type", None)
        if not t:
            continue
        data = {}
        for _k in ("text", "file", "url", "base64", "path", "name", "id", "qq"):
            _v = getattr(s, _k, None)
            if _v:
                _v = str(_v)
                data[_k] = _v[:120] + ("..." if len(_v) > 120 else "")
        out.append({"type": t, "data": data})
    return out


def _log_onebot11(logger, stage: str, segments) -> None:
    """记录 OneBot11 发送 JSON 结构 (发送失败可倒查)."""
    try:
        import json

        segs = _onebot11_segments(segments)
        logger.info(
            f"[ParserLite] onebot11 send [{stage}]: {json.dumps(segs, ensure_ascii=False)}"
        )
    except Exception:
        pass


def get_sendable_types() -> list[str]:
    """动态扫描上游 ContentItem Union 成员 → 可发送类型列表 (0 hardcode)."""
    from nonebot_plugin_parser_lite.data import ContentItem

    mapping = {}
    for cls in getattr(ContentItem, "__args__", []) or []:
        name = getattr(cls, "__name__", "")
        if name in ("ImageContent", "VideoContent", "AudioContent"):
            mapping[name] = {
                "ImageContent": "image",
                "VideoContent": "video",
                "AudioContent": "audio",
            }[name]
    return list(dict.fromkeys(["card", *mapping.values()]))


def should_send(media_type: str) -> bool:
    """发送策略门 (配置驱动, 默认全类型)."""
    try:
        s = bridge_cfg("send_strategy", get_sendable_types())
        if isinstance(s, str):
            import json

            try:
                s = json.loads(s)
            except Exception:
                s = get_sendable_types()
        return media_type in (s if isinstance(s, list) else [])
    except Exception:
        return True


async def send_media_file(
    event,
    path,
    media_type: str,
    source_url: str = "",
    converters: dict | None = None,
    logger=None,
    cover_path: str = "",
) -> SendReport:
    """媒体发送入口 — fail-fast 预算 (P2-8): 每级 15s, 总 60s.

    内部 event.send 包装为受限调用: 单级卡死不耗尽全部时间预算.
    """
    import asyncio as _aio
    import time as _time

    _orig_send = event.send
    _deadline = _time.monotonic() + 60.0

    async def _bounded_send(segs):
        _remain = _deadline - _time.monotonic()
        if _remain <= 0:
            raise TimeoutError("媒体发送预算耗尽 (60s)")
        await _aio.wait_for(_orig_send(segs), timeout=min(15.0, _remain))

    event.send = _bounded_send
    try:
        return await _send_media_impl(
            event, path, media_type, source_url, converters, logger, cover_path
        )
    finally:
        event.send = _orig_send


async def _send_media_impl(
    event,
    path,
    media_type: str,
    source_url: str = "",
    converters: dict | None = None,
    logger=None,
    cover_path: str = "",
) -> SendReport:
    """媒体发送骨架 (md5 秒传 → 正常路径多级 failback).

    :param converters: 扩展回调 {"video": async fn(path)->Path, "audio": async fn(path)->Path}
                       — 由调用方注入 (FFmpeg 转换; r11: image 键不再消费, 直发原生路径)
    :param cover_path: 视频封面路径 (OneBot11 视频+封面链, 上游 VideoContent.cover)
    :return: SendReport (bool(report)=ok, 兼容旧调用)

    OneBot11 发送语义 (参考协议与 AstrBot 组件事实):
    - image: use_base64 → fromBytes(base64://), 否则 fromFileSystem → fromURL
    - video: 空文件拦截; >file_threshold_mb → Comp.File 文件发送 (OneBot11 大文件必失败);
             base64 估算 >20MB → Comp.File; 否则 Video.fromBase64 + 封面链
    - audio: Record.fromBase64 (组件无 fromBytes)
    """
    _report = SendReport(stage="media")
    import base64
    from pathlib import Path

    if logger is None:
        import logging

        logger = logging.getLogger("parser-lite.bridge.send")

    p = Path(path)
    if not p.exists():
        logger.warning(f"[ParserLite] send_media_file: file missing {p}")
        _report.errors.append(f"file missing: {p}")
        return _report
    try:
        p.chmod(0o644)
    except Exception:
        pass
    if p.stat().st_size == 0:
        logger.warning(f"[ParserLite] send_media_file: empty file {p}")
        _report.errors.append(f"empty file: {p.name}")
        return _report
    converters = converters or {}
    _Cmps = _get_components()
    File, Image, Record, Video = (
        _Cmps["File"],
        _Cmps["Image"],
        _Cmps["Record"],
        _Cmps["Video"],
    )

    def _try_send(stage: str, segs, exc: Exception | None = None):
        _log_onebot11(logger, stage, segs)
        if exc is not None:
            _report.errors.append(f"{stage}: {type(exc).__name__}: {exc}")

    async def _send_file_stage(stage: str, p_: Path) -> bool:
        """OneBot11 文件发送 (视频/音频超限兜底)."""
        try:
            _segs = [File(name=p_.name, file=p_.as_uri())]
            await event.send(event.chain_result(_segs))
            _try_send(stage, _segs)
            _report.ok, _report.stage = True, stage
            _report.segments = _onebot11_segments(_segs)
            return _report
        except Exception as _e:
            _try_send(stage, [], _e)
            return _report

    # 上游 use_base64 (原始调用优先): true → 强制 base64 发送
    # 读取单一事实来源 (global_source, B7: 顶部 import, 无重复软导入)
    try:
        _use_b64 = bool(bridge_cfg("plite_use_base64", False))
    except Exception:
        _use_b64 = False

    _fsz = 0

    def _ok(stage: str, segs) -> SendReport:
        """发送成功统一出口: 反馈记录."""
        _try_send(stage, segs)
        _report.ok, _report.stage = True, stage
        _report.segments = _onebot11_segments(segs)
        return _report

    if media_type == "image":
        # base64 优先 (上游配置驱动)
        if _use_b64:
            try:
                raw = p.read_bytes()
                _segs = [Image.fromBytes(raw)]
                await event.send(event.chain_result(_segs))
                return _ok("image-b64", _segs)
            except Exception as _e:
                _try_send("image-b64", [], _e)
        try:
            _segs = [Image.fromFileSystem(str(p))]
            await event.send(event.chain_result(_segs))
            return _ok("image-fs", _segs)
        except Exception as _e:
            _try_send("image-fs", [], _e)
        try:
            raw = p.read_bytes()
            _segs = [Image.fromBytes(raw)]
            await event.send(event.chain_result(_segs))
            return _ok("image-bytes", _segs)
        except Exception as _e:
            _try_send("image-bytes", [], _e)
        if source_url:
            try:
                _segs = [Image.fromURL(source_url)]
                await event.send(event.chain_result(_segs))
                return _ok("image-url", _segs)
            except Exception as _e:
                _try_send("image-url", [], _e)

    elif media_type == "video":
        conv = converters.get("video")
        mp4 = await conv(p) if conv else p
        mp4 = Path(mp4)
        _fsz = mp4.stat().st_size if mp4.exists() else 0
        if _fsz == 0:
            _report.errors.append("empty video")
            return _report
        # 大文件阈值: 超限 → Comp.File 文件发送 (OneBot11 base64 大视频必失败)
        try:
            _th_mb = int(bridge_cfg("plite_video_file_threshold_mb", 100) or 100)
        except Exception:
            _th_mb = 100
        if _fsz > _th_mb * 1024 * 1024:
            if await _send_file_stage("video-file", mp4):
                return _report
        # base64 估算 >20MB → File (OneBot11 单消息上限)
        if _fsz * 4 / 3 > 20 * 1024 * 1024:
            if await _send_file_stage("video-file-big", mp4):
                return _report
        # 视频 + 封面链 (OneBot11 常见组合, cover_path 由调用方注入)
        _segs = []
        if cover_path and Path(cover_path).exists():
            try:
                _segs.append(Image.fromFileSystem(str(cover_path)))
            except Exception:
                pass
        if _use_b64:
            try:
                raw = mp4.read_bytes()
                b64 = base64.b64encode(raw).decode()
                _segs = [*_segs, Video.fromBase64(b64)]
                await event.send(event.chain_result(_segs))
                return _ok("video-b64", _segs)
            except Exception as _e:
                _try_send("video-b64", [], _e)
        try:
            _segs = [*_segs, Video.fromFileSystem(str(mp4))]
            await event.send(event.chain_result(_segs))
            return _ok("video-fs", _segs)
        except Exception as _e:
            _try_send("video-fs", [], _e)
        try:
            raw = mp4.read_bytes()
            b64 = base64.b64encode(raw).decode()
            _segs = [Video.fromBase64(b64)]
            await event.send(event.chain_result(_segs))
            return _ok("video-b64-fallback", _segs)
        except Exception as _e:
            _try_send("video-b64-fallback", [], _e)
        if source_url:
            try:
                _segs = [Video.fromURL(source_url)]
                await event.send(event.chain_result(_segs))
                return _ok("video-url", _segs)
            except Exception as _e:
                _try_send("video-url", [], _e)

    elif media_type == "audio":
        conv = converters.get("audio")
        mp3 = await conv(p) if conv else p
        mp3 = Path(mp3)
        if _use_b64:
            try:
                raw = mp3.read_bytes()
                b64 = base64.b64encode(raw).decode()
                _segs = [Record.fromBase64(b64)]
                await event.send(event.chain_result(_segs))
                return _ok("audio-b64", _segs)
            except Exception as _e:
                _try_send("audio-b64", [], _e)
        try:
            _segs = [Record.fromFileSystem(str(mp3))]
            await event.send(event.chain_result(_segs))
            return _ok("audio-fs", _segs)
        except Exception as _e:
            _try_send("audio-fs", [], _e)
        try:
            raw = mp3.read_bytes()
            b64 = base64.b64encode(raw).decode()
            _segs = [Record.fromBase64(b64)]  # 组件无 fromBytes (OneBot11 record.file)
            await event.send(event.chain_result(_segs))
            return _ok("audio-b64-fallback", _segs)
        except Exception as _e:
            _try_send("audio-b64-fallback", [], _e)
        if source_url:
            try:
                _segs = [Record.fromURL(source_url)]
                await event.send(event.chain_result(_segs))
                return _ok("audio-url", _segs)
            except Exception as _e:
                _try_send("audio-url", [], _e)
    return _report


# ── 发送管线 (r11: main.py 下沉, 命令/事件共用) ──────────────────────────────


def _logger():
    import logging

    return logging.getLogger("parser-lite.bridge.send")


async def convert_audio(p: Path, fmt: str = "mp3") -> Path:
    """FFmpeg 音频转码: → MP3 (128k) 或 AMR (8k mono)."""
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

    if not await FFmpeg.is_available():
        return p
    if p.suffix.lower() in (".mp3", ".m4a", ".aac", ".wav") and fmt == "mp3":
        return p
    out = p.parent / f"{p.stem}_cvt.{fmt}"
    if out.exists():
        return out
    opts = (
        [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(p),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            str(out),
        ]
        if fmt == "mp3"
        else [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(p),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-b:a",
            "12.2k",
            str(out),
        ]
    )
    try:
        await FFmpeg.exec_ffmpeg(opts)
        return out
    except Exception:
        return p


async def convert_video(p: Path) -> Path:
    """FFmpeg 视频转封装/转码: → H.264 + AAC in MP4."""
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

    if not await FFmpeg.is_available():
        return p
    if p.suffix.lower() == ".mp4":
        return p
    out = p.parent / f"{p.stem}_cvt.mp4"
    if out.exists():
        return out
    try:
        await FFmpeg.exec_ffmpeg(
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(p),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(out),
            ]
        )
        return out
    except Exception:
        return p


def _default_converters() -> dict:
    """媒体转换器 (r11: 仅 video/audio — FFmpeg; image 直发原生路径)."""
    return {
        "video": convert_video,
        "audio": lambda path: convert_audio(path, fmt="mp3"),
    }


async def resolve_cover_path(item) -> Path | None:
    """视频封面路径: 优先上游 cover path_task, 失败返回 None."""
    try:
        _cover = getattr(item, "cover", None)
        if _cover is not None and getattr(_cover, "path_task", None) is not None:
            _cp = Path(str(await _cover.path_task))
            if _cp.exists():
                return _cp
    except Exception:
        pass
    return None


async def try_direct_send(event, item, src_url: str) -> bool:
    """F5: HEAD+Range 探测大小, 未超限则 URL 直发 (免下载). 失败返回 False."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.head(src_url, headers={"Range": "bytes=0-0"})
            size = None
            cr = resp.headers.get("content-range", "")
            cl = resp.headers.get("content-length", "")
            if cr and "/" in cr:
                size = int(cr.split("/")[-1])
            elif cl and cl.isdigit():
                size = int(cl)
        if size is None:
            return False
        from bridge.config import get_config

        max_mb = int(get_config().max_size)
        if size > max_mb * 1024 * 1024:
            return False  # 超限回退下载
        from astrbot.api.message_components import Image, Video

        from nonebot_plugin_parser_lite.data import (
            GraphicContent,
            ImageContent,
            VideoContent,
        )

        if isinstance(item, VideoContent):
            if should_send("video"):
                await event.send(event.chain_result([Video.fromURL(src_url)]))
            return True
        if isinstance(item, (ImageContent, GraphicContent)):
            if should_send("image"):
                await event.send(event.chain_result([Image.fromURL(src_url)]))
            return True
        return False
    except Exception:
        return False


async def _send_media(
    event, p: Path, media_type: str, source_url: str = "", cover_path: str = ""
):
    """send_media_file 封装: 默认转换器 + 失败回显."""
    report = await send_media_file(
        event,
        p,
        media_type,
        source_url,
        _default_converters(),
        _logger(),
        cover_path=cover_path,
    )
    if report:
        return
    try:
        _why = "; ".join(report.errors)[:300] or "OneBot API 不可用"
        from astrbot.api.message_components import Plain

        await event.send(
            event.chain_result([Plain(f"[ParserLite] {media_type} 发送失败: {_why}")])
        )
    except Exception:
        pass


async def send_video_cover(event, item) -> None:
    """F6: 视频仅发封面 — 优先上游 cover, 无则 ffmpeg 截帧兜底."""
    try:
        _cp = await resolve_cover_path(item)
        if _cp is not None:
            if should_send("image"):
                await _send_media(
                    event,
                    _cp,
                    "image",
                    source_url=getattr(item.path_task, "url", ""),
                )
            return
        from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

        if not await FFmpeg.is_available():
            return
        vpath = Path(str(await item.path_task))
        cover = vpath.parent / f"{vpath.stem}_cover.jpg"
        await FFmpeg.exec_ffmpeg(
            [
                "-i",
                str(vpath),
                "-frames:v",
                "1",
                "-q:v",
                "5",
                "-y",
                str(cover),
            ]
        )
        if cover.exists():
            if should_send("image"):
                await _send_media(
                    event,
                    cover,
                    "image",
                    source_url=getattr(item.path_task, "url", ""),
                )
            cover.unlink(missing_ok=True)
    except Exception:
        pass


async def send_as_forward(event, items: list, result) -> None:
    """合并转发: 多项媒体打包 Comp.Nodes (移植自上游 Renderer.__build_forward_segs)."""
    from astrbot.api.message_components import (
        Image,
        Node,
        Nodes,
        Plain,
        Record,
        Video,
    )

    from bridge.send import send_with_fallback
    from nonebot_plugin_parser_lite.data import AudioContent, ImageContent, VideoContent

    nodes = []
    author = result.author.name if result.author and result.author.name else "解析"
    platform = result.platform.display_name if result.platform else ""
    MAX_PER_NODE = int(bridge_cfg("plite_forward_max_nodes", 90) or 90)

    for item in items:
        if not hasattr(item, "path_task"):
            continue
        if len(nodes) >= MAX_PER_NODE:
            break
        try:
            p = Path(str(await item.path_task))
            if isinstance(item, ImageContent):
                nodes.append(
                    Node(
                        content=[
                            Plain(f"{author} | {platform}"),
                            Image.fromFileSystem(str(p)),
                        ],
                        name=author,
                        uin="0",
                    )
                )
            elif isinstance(item, VideoContent):
                nodes.append(
                    Node(
                        content=[
                            Plain(f"{author} 的视频"),
                            Video.fromFileSystem(str(p)),
                        ],
                        name=author,
                        uin="0",
                    )
                )
            elif isinstance(item, AudioContent):
                nodes.append(
                    Node(
                        content=[
                            Plain(f"{author} 的音频"),
                            Record.fromFileSystem(str(p)),
                        ],
                        name=author,
                        uin="0",
                    )
                )
        except Exception:
            pass

    if nodes:
        # E4: 发送降级链 — 合并转发失败 → 逐项单发
        async def _try_forward() -> bool:
            await event.send(event.chain_result([Nodes(nodes=nodes)]))
            return True

        async def _try_individual() -> bool:
            sent_any = False
            for _node in nodes:
                for _seg in getattr(_node, "content", []) or []:
                    try:
                        await event.send(event.chain_result([_seg]))
                        sent_any = True
                    except Exception:
                        pass
            return sent_any

        await send_with_fallback(
            try_send=_try_forward,
            fallbacks=[_try_individual],
            logger=_logger(),
            label="合并转发",
        )


async def send_one(event, item) -> None:
    """发送单个媒体项 (直链/仅封面/按类型分派)."""
    if not hasattr(item, "path_task"):
        return
    try:
        src_url = getattr(item.path_task, "url", "")
        dur = getattr(item, "duration", 0.0)
        _direct = bool(bridge_cfg("plite_direct_link", False))
        _cover_only = bool(bridge_cfg("plite_send_cover_only", False))
        from nonebot_plugin_parser_lite.data import (
            AudioContent,
            GraphicContent,
            ImageContent,
            StickerContent,
            VideoContent,
        )

        # F5: 直链免下载模式 (配置驱动)
        if _direct and src_url:
            sent = await try_direct_send(event, item, src_url)
            if sent:
                return
        # F6: 视频仅发封面 (配置驱动)
        if isinstance(item, VideoContent) and _cover_only:
            if should_send("image"):
                await send_video_cover(event, item)
            return
        p = Path(str(await item.path_task))
        _cover_p = ""
        if isinstance(item, VideoContent):
            _cover_p = str(await resolve_cover_path(item)) or ""
        if isinstance(item, (ImageContent, GraphicContent, StickerContent)):
            if should_send("image"):
                await _send_media(event, p, "image", source_url=src_url)
        elif isinstance(item, VideoContent):
            if should_send("video"):
                await _send_media(
                    event,
                    p,
                    "video",
                    source_url=src_url,
                    cover_path=_cover_p,
                )
        elif isinstance(item, AudioContent):
            if should_send("audio"):
                await _send_media(event, p, "audio", source_url=src_url, duration=dur)
    except Exception:
        pass


async def send_items(event, items: list, result) -> None:
    """统一发送入口: 超过4项且配置允许 → 合并转发, 否则逐一发送."""
    from bridge.config import get_config

    need_forward = (
        get_config().need_forward_contents
        and len([i for i in items if hasattr(i, "path_task")]) > 4
    )
    if need_forward:
        await send_as_forward(event, items, result)
    else:
        for item in items:
            await send_one(event, item)


async def dispatch_result(event, result) -> None:
    """解析结果统一分派: should_send("card") → send_card + send_items.

    命令与消息事件共用 (send_card 的文本回退在 send_card 内部).
    """
    if should_send("card"):
        from bridge.render import send_card
        from bridge.send import format_full

        await send_card(event, result, format_full)
    await send_items(event, result.content, result)


# ── 格式化 (format 合并) ─────────────────────────────────────
def _safe_label(result: ParseResult) -> str:
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


# ── 发送降级 (fallback 合并) ───────────────────────────────────
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
