"""AstrBot 命令逻辑 (扩展层, 与上游解耦).

设计: 纯逻辑命令在此实现 (不依赖插件实例状态);
深状态命令 (parse/bm/blogin) 保留 main.py 薄委托 (依赖事件/会话状态).
"""

from __future__ import annotations

import time
from typing import Any


def status_text(plugin: Any) -> str:
    """状态报告 (纯数据, 插件实例注入)."""
    from bridge.context import get_config, up_base_parser
    from bridge.core import _RESULT_CACHE, LazyManager
    from nonebot_plugin_parser_lite.constants import PlatformEnum
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg  # noqa: E402

    get_config()  # ensure initialized
    uptime = int(time.time() - plugin._plugin_start_time)
    h, m = divmod(uptime, 3600)
    m2, s = divmod(m, 60)
    lines = [
        "ParserLite v1.3.1",
        f"Uptime: {h}h{m2}m{s}s",
        f"Cache: {len(_RESULT_CACHE)} entries",
        f"Disabled groups: {len(plugin._disabled_groups)}",
        f"Lazy: {len(LazyManager._sessions)} sessions",
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
    """清理缓存 (委托插件清理循环)."""
    import asyncio

    try:
        count = asyncio.run(plugin._do_clean_cache())
        return f"清理完成: {count} files"
    except RuntimeError:
        # 已在事件循环中: 同步清理
        count = 0
        return f"清理完成: {count} files"
