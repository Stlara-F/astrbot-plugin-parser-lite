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

_COMPONENTS_CACHE: dict | None = None


def _get_components():
    """延迟获取 AstrBot 组件 (缓存, 避免重复 import 触发 AstrBot
    sqlalchemy 表重复定义; CI/测试可注入假组件)."""
    global _COMPONENTS_CACHE
    if _COMPONENTS_CACHE is None:
        from astrbot.api.message_components import File, Image, Record, Video

        _COMPONENTS_CACHE = {"File": File, "Image": Image, "Record": Record, "Video": Video}
    return _COMPONENTS_CACHE

# 最近一次发送报告 (OneBot11 发送反馈回显: 成功/失败 + 段结构 + 原因)
last_send_report: dict = {"ok": None, "segments": [], "errors": [], "stage": ""}


def reset_send_report() -> None:
    last_send_report.update({"ok": None, "segments": [], "errors": [], "stage": ""})


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
        logger.info(f"[ParserLite] onebot11 send [{stage}]: {json.dumps(segs, ensure_ascii=False)}")
        last_send_report["segments"] = segs
    except Exception:
        pass


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

    # 确保渲染补丁已应用 (safe_src 默认 method + pl_esc/pl_str 注册, 幂等)
    try:
        from bridge.render_patch import apply_render_patch

        apply_render_patch()
    except Exception:
        pass

    cache_key = result.url
    if cache_key in _CARD_CACHE:
        data = _CARD_CACHE.pop(cache_key)
        _CARD_CACHE[cache_key] = data
        _segs = [_image_from_bytes(data)]
        _log_onebot11(logger, "card-cache", _segs)
        await event.send(event.chain_result(_segs))
        logger.info(f"[ParserLite] card cache hit ({len(data)} bytes)")
        last_send_report.update({"ok": True, "stage": "card-cache"})
        return True

    try:
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
        last_send_report.update({"ok": True, "stage": "card"})
        return True
    except Exception as _e:
        _reason = f"{type(_e).__name__}: {_e}"
        logger.warning(f"[ParserLite] 卡片渲染失败, 回退文本 ({_reason})")
        last_send_report["errors"].append(f"render: {_reason}")
        try:
            _segs = [_plain(format_full(result))]
            _log_onebot11(logger, "card-fallback", _segs)
            await event.send(event.chain_result(_segs))
            last_send_report.update({"ok": True, "stage": "card-fallback"})
            return True
        except Exception as _e2:
            _reason2 = f"{type(_e2).__name__}: {_e2}"
            logger.error(f"[ParserLite] 回退文本发送也失败 (OneBot API 可能不可用): {_reason2}")
            last_send_report.update({"ok": False, "stage": "card-fallback"})
            last_send_report["errors"].append(f"send: {_reason2}")
            return False


def _image_from_bytes(data: bytes):
    """AstrBot 图片组件 (延迟 import, CI 无 astrbot 时可导入本模块)."""
    from astrbot.api.message_components import Image

    return Image.fromBytes(data)


def _plain(text: str):
    from astrbot.api.message_components import Plain

    return Plain(text)


async def send_media_file(event, path, media_type: str, source_url: str = "",
                          converters: dict | None = None, logger=None,
                          cover_path: str = "") -> bool:
    """媒体三路发送骨架 (fromFileSystem → bytes/base64 → fromURL).

    :param converters: 扩展回调 {"image": async fn(path)->bytes, "video": async fn(path)->Path,
                       "audio": async fn(path)->Path} — 由调用方注入 (FFmpeg 转换保持扩展)
    :param cover_path: 视频封面路径 (OneBot11 视频+封面链, 上游 VideoContent.cover)
    :return: 是否成功发送

    OneBot11 发送语义 (参考协议与 AstrBot 组件事实):
    - image: use_base64 → fromBytes(base64://), 否则 fromFileSystem → 压缩 → fromURL
    - video: 空文件拦截; >file_threshold_mb → Comp.File 文件发送 (OneBot11 大文件必失败);
             base64 估算 >20MB → Comp.File; 否则 Video.fromBase64 + 封面链
    - audio: Record.fromBase64 (组件无 fromBytes)
    """
    import base64
    from pathlib import Path

    if logger is None:
        import logging
        logger = logging.getLogger("parser-lite.bridge.send")

    p = Path(path)
    if not p.exists():
        logger.warning(f"[ParserLite] send_media_file: file missing {p}")
        last_send_report.update({"ok": False, "stage": "media", "errors": [f"file missing: {p}"]})
        return False
    try:
        p.chmod(0o644)
    except Exception:
        pass
    if p.stat().st_size == 0:
        logger.warning(f"[ParserLite] send_media_file: empty file {p}")
        last_send_report.update({"ok": False, "stage": "media", "errors": [f"empty file: {p.name}"]})
        return False
    converters = converters or {}
    _Cmps = _get_components()
    File, Image, Record, Video = _Cmps["File"], _Cmps["Image"], _Cmps["Record"], _Cmps["Video"]

    reset_send_report()
    last_send_report["stage"] = f"media:{media_type}"

    def _try_send(stage: str, segs, exc: Exception | None = None):
        _log_onebot11(logger, stage, segs)
        if exc is not None:
            last_send_report["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")

    async def _send_file_stage(stage: str, p_: Path) -> bool:
        """OneBot11 文件发送 (视频/音频超限兜底)."""
        try:
            _segs = [File(name=p_.name, file=p_.as_uri())]
            await event.send(event.chain_result(_segs))
            _try_send(stage, _segs)
            last_send_report["ok"] = True
            return True
        except Exception as _e:
            _try_send(stage, [], _e)
            return False

    # 上游 use_base64 (原始调用优先): true → 强制 base64 发送
    # 读取单一事实来源 _source (与 read_cfg 一致, 避免上游单例缓存污染)
    try:
        from bridge.cfg import read_cfg as _read_cfg
        from bridge.context import BridgeConfig as _BC

        _use_b64 = bool(_read_cfg(_BC._source or {}, "plite_use_base64", False))
    except Exception:
        _use_b64 = False


    _m5 = None
    _md5_failed = None
    _fsz = 0

    def _ok(stage: str, segs) -> bool:
        """发送成功统一出口: 反馈 + md5 缓存记录 (秒传命中后不再重复记录)."""
        _try_send(stage, segs)
        last_send_report["ok"] = True
        if _m5 and _md5_failed != _m5:
            try:
                from bridge.media_cache import get_cache as _gc

                _gc().put(_m5, media_type, _fsz if _fsz else p.stat().st_size)
            except Exception:
                pass
        return True
    # md5 秒传: 有缓存指纹 → file://md5 引用 (QQ 服务器资源秒回应, 参考
    # SnowLuma fast-upload; 失败 → 回退正常路径, 多级 failback)
    try:
        from bridge.cfg import read_cfg as _rc2
        from bridge.context import BridgeConfig as _BC2
        from bridge.media_cache import compute_md5, get_cache, md5_file_ref

        _md5_fast = bool(_rc2(_BC2._source or {}, "plite_md5_fast_send", True))
        if _md5_fast and media_type in ("image", "video", "audio"):
            _m5 = compute_md5(p)
            if get_cache().has(_m5):
                _ref = md5_file_ref(_m5)
                if media_type == "image":
                    _md5_seg = Image(file=_ref)
                elif media_type == "video":
                    _md5_seg = Video(file=_ref)
                else:
                    _md5_seg = Record(file=_ref)
                try:
                    await event.send(event.chain_result([_md5_seg]))
                    _try_send(f"{media_type}-md5", [_md5_seg])
                    last_send_report["ok"] = True
                    return True
                except Exception as _e:
                    _try_send(f"{media_type}-md5", [], _e)
                    _md5_failed = _m5  # 引用失败, 后续正常路径重试
        else:
            _m5 = None
    except Exception:
        _m5 = None

    if media_type == "image":
        # base64 优先 (上游配置驱动)
        if _use_b64:
            try:
                raw = p.read_bytes()
                compress = converters.get("image")
                if compress is not None:
                    raw = await compress(p)
                _segs = [Image.fromBytes(raw)]
                await event.send(event.chain_result(_segs))
                _try_send("image-b64", _segs)
                last_send_report["ok"] = True
                return True
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
            compress = converters.get("image")
            if compress is not None:
                raw = await compress(p)
            _segs = [Image.fromBytes(raw)]
            await event.send(event.chain_result(_segs))
            return _ok("image-bytes", _segs)
        except Exception as _e:
            _try_send("image-bytes", [], _e)
        if source_url:
            try:
                _segs = [Image.fromURL(source_url)]
                await event.send(event.chain_result(_segs))
                _try_send("image-url", _segs)
                last_send_report["ok"] = True
                return True
            except Exception as _e:
                _try_send("image-url", [], _e)

    elif media_type == "video":
        conv = converters.get("video")
        mp4 = await conv(p) if conv else p
        mp4 = Path(mp4)
        _fsz = mp4.stat().st_size if mp4.exists() else 0
        if _fsz == 0:
            last_send_report.update({"ok": False, "stage": "video", "errors": ["empty video"]})
            return False
        # 大文件阈值: 超限 → Comp.File 文件发送 (OneBot11 base64 大视频必失败)
        try:
            from bridge.cfg import read_cfg as _read_cfg
            from bridge.context import BridgeConfig as _BC

            _th_mb = int(_read_cfg(_BC._source or {}, "plite_video_file_threshold_mb", 100) or 100)
        except Exception:
            _th_mb = 100
        if _fsz > _th_mb * 1024 * 1024:
            if await _send_file_stage("video-file", mp4):
                return True
        # base64 估算 >20MB → File (OneBot11 单消息上限)
        if _fsz * 4 / 3 > 20 * 1024 * 1024:
            if await _send_file_stage("video-file-big", mp4):
                return True
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
                _try_send("video-b64", _segs)
                last_send_report["ok"] = True
                return True
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
                _try_send("video-url", _segs)
                last_send_report["ok"] = True
                return True
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
                _try_send("audio-b64", _segs)
                last_send_report["ok"] = True
                return True
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
                _try_send("audio-url", _segs)
                last_send_report["ok"] = True
                return True
            except Exception as _e:
                _try_send("audio-url", [], _e)
    last_send_report["ok"] = False
    return False
