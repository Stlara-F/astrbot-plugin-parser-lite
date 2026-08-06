"""AstrBot 命令逻辑 (扩展层, 与上游解耦).

设计: 纯逻辑命令在此实现 (不依赖插件实例状态);
深状态命令 (parse/bm/blogin) 保留 main.py 薄委托 (依赖事件/会话状态).
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
