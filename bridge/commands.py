"""AstrBot 命令业务 (扩展层, 与上游解耦; r11: main.py 全委托).

设计: 命令函数 (plugin, event) 注入模式, 返回 AsyncIterator 逐条 yield
消息段; main.py 仅 `async for msg in commands.x(self, event): yield msg`
薄委托转发, 事件/命令注册 (filter.command 类方法绑定) 保持.
"""

from __future__ import annotations

import time
from typing import Any


def status_text(plugin: Any) -> str:
    """状态报告 (纯数据, 插件实例注入; r8: 自研缓存/懒下载会话已删)."""
    from bridge.context import get_config, up_base_parser
    from nonebot_plugin_parser_lite.constants import PlatformEnum
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg  # noqa: E402

    get_config()  # ensure initialized
    uptime = int(time.time() - plugin._plugin_start_time)
    h, m = divmod(uptime, 3600)
    m2, s = divmod(m, 60)
    lines = [
        "ParserLite v1.3.2",
        f"Uptime: {h}h{m2}m{s}s",
        f"Disabled groups: {len(plugin._disabled_groups)}",
        f"Platforms: {len(PlatformEnum)}",
        f"Parsers: {len(list(up_base_parser().get_all_subclass()))}",
    ]
    try:
        lines.append(f"FFmpeg: is_available={FFmpeg.is_available}")
    except Exception as e:
        lines.append(f"FFmpeg: {e}")
    return "\n".join(lines)


def toggle_group(plugin: Any, gid: str, enable: bool) -> str:
    """群启用/禁用解析 (持久化 disabled_groups)."""
    from bridge.core import _save_disabled_groups

    if enable:
        plugin._disabled_groups.discard(gid)
    else:
        plugin._disabled_groups.add(gid)
    _save_disabled_groups(plugin._disabled_groups)
    return "本群解析已开启" if enable else "本群解析已关闭"


def clean_cache(plugin: Any) -> str:
    """清理缓存 (r8: 委托上游 clear_result_cache + CacheManager)."""
    import asyncio

    try:
        from nonebot_plugin_parser_lite.pipeline import clear_result_cache
        from nonebot_plugin_parser_lite.utils.cache import CacheManager

        async def _clean():
            await CacheManager.clean_expired()
            clear_result_cache()

        try:
            asyncio.get_running_loop()
            _t = asyncio.create_task(_clean())
            _t.add_done_callback(lambda _t2: None)  # 持有引用防 GC
            return "缓存清理已触发"
        except RuntimeError:
            asyncio.run(_clean())
            return "缓存清理完成"
    except Exception as e:
        return f"缓存清理失败: {e}"


# ── 判定 (r11: main.py 事件判定内聚, 命令与消息事件共用) ─────────────────────


def gid(event) -> str:
    """群 ID 归一 (unified_msg_origin 尾段, 失败回退 unknown)."""
    try:
        origin = event.unified_msg_origin
        return origin.split(":")[-1] if origin and ":" in origin else "unknown"
    except Exception:
        return "unknown"


def is_disabled(plugin, event) -> bool:
    """群禁用判定 (plugin._disabled_groups 持久化状态)."""
    return gid(event) in plugin._disabled_groups


def is_blacklisted(plugin, event) -> bool:
    """黑名单判定 (上游 Config.blacklist_users)."""
    from bridge.context import get_config

    try:
        return event.get_sender_id() in get_config().blacklist_users
    except Exception:
        return False


def _extract_urls(event) -> list[str]:
    """消息 URL 提取 (主链路 + 引用消息逃生通道)."""
    import astrbot.api.message_components as _Comp

    from bridge.url_extract import extract_reply_urls, extract_urls

    urls = extract_urls(event, _Comp)
    if not urls:
        urls = extract_reply_urls(event)
    return urls


# ── 命令业务 (r11: main.py 下沉) ─────────────────────────────────────────────


async def parse(plugin, event):
    """parse 命令业务: 判定 → URL 提取 → 解析 → dispatch_result."""
    if is_blacklisted(plugin, event) or is_disabled(plugin, event):
        yield event.plain_result("本群已禁用")
        return
    urls = _extract_urls(event)
    if not urls:
        yield event.plain_result("未找到链接")
        return
    url = urls[0]
    import logging

    logging.getLogger("nonebot_plugin_parser_lite").info(
        f"[ParserLite] cmd_parse: {url[:120]}"
    )
    try:
        result = await plugin._parse_raw(url)
        if result is None:
            yield event.plain_result("不支持的链接")
            return
        from bridge.send import dispatch_result

        await dispatch_result(event, result)
    except Exception as e:
        import traceback

        logging.getLogger("nonebot_plugin_parser_lite").error(
            f"[ParserLite] cmd_parse 异常\n{traceback.format_exc()}"
        )
        yield event.plain_result(f"解析失败: {e}")


async def bm(plugin, event):
    """bm 命令业务: BV 正则提取 → BilibiliParser.extract_download_urls → aclose."""
    import re as _re

    from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser

    text = event.get_message_str()
    bvid = None
    m = _re.search(r"[Bb][Vv][A-Za-z0-9]{10}", text)
    if m:
        bvid = m.group(0)
    # 从被回复的消息中提取 BV (上游 BvReplyMergeExtension 等价实现)
    if not bvid:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            raw_segs = getattr(msg_obj, "message", None) or []
            for seg in raw_segs if isinstance(raw_segs, list) else []:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    reply_data = seg.get("data", {})
                    reply_text = (
                        reply_data.get("text", "")
                        or reply_data.get("message", "")
                        or ""
                    )
                    m = _re.search(r"[Bb][Vv][A-Za-z0-9]{10}", str(reply_text))
                    if m:
                        bvid = m.group(0)
                        break
    if not bvid:
        yield event.plain_result("未找到BV号 (当前消息/回复消息均无)")
        return
    bili = BilibiliParser()
    try:
        urls = await bili.extract_download_urls(bvid=bvid)
        _video_url, audio_url = (urls[0], urls[1]) if len(urls) > 1 else (urls[0], None)
        if audio_url:
            yield event.plain_result(f"Audio: {audio_url[:80]}")
        else:
            yield event.plain_result("该视频未提取到独立音频流")
    except Exception as e:
        yield event.plain_result(f"Error: {e}")
    finally:
        await bili.aclose()


async def blogin(plugin, event):
    """blogin 命令业务: login_with_qrcode → 二维码段."""
    from astrbot.api.message_components import Image

    from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser

    bili = BilibiliParser()
    try:
        qr_bytes = await bili.login_with_qrcode()
        yield event.plain_result("B站登录二维码已生成, 请用手机B站扫描以下二维码:")
        yield event.chain_result([Image.fromBytes(qr_bytes)])
    except Exception as e:
        yield event.plain_result(f"Error: {e}")


async def install_chromium(plugin, event):
    """install_chromium 命令业务: 委托 chromium.ensure_chromium 逐条回显."""
    from pathlib import Path

    from bridge.chromium import ensure_chromium
    from bridge.context import get_config

    ok, messages = await ensure_chromium(
        browsers_path=str(Path(get_config().data_dir) / "playwright_browsers"),
        started_msg="Chromium 已可用, 无需重复安装",
    )
    for msg in messages:
        yield event.plain_result(msg)
    _ = ok


async def doctor(plugin, event):
    """doctor 命令业务: run_checks/summarize/render_text/save_snapshot + 修复建议."""
    try:
        from bridge.doctor import render_text, run_checks, save_snapshot, summarize

        results = await run_checks()
        summary = summarize(results)
        report = render_text(results, summary)
        if summary["failed"] or summary["warn"]:
            snap = save_snapshot(results, summary)
            report += "\n\n── 修复建议 ──"
            report += "\n  1. Config/Downloader 失败 → 检查插件配置与依赖"
            report += "\n  2. Chromium 警告 → 发送 parse_install_chromium"
            report += "\n  3. Network 失败 → 检查代理/网络"
            report += "\n  4. 其余失败 → 查看上方 error 详情"
            if snap:
                report += f"\n\n快照已保存: {snap}"
        yield event.plain_result(report)
    except Exception as e:
        yield event.plain_result(f"doctor 执行失败: {e}")


async def parse_url_cmd(plugin, event, url: str) -> str:
    """parse_url 业务 (llm_tool, 返回 str)."""
    if is_blacklisted(plugin, event):
        return "黑名单用户"
    result = await plugin._parse_and_format(url)
    return result or "无法解析该链接"
