#!/usr/bin/env python3
"""
AstrBot adapter for nonebot-plugin-parser-lite.
PR#205 merged → sokoko-org/main. Runs inside nonebot_plugin_parser_lite/ package.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys
import time
import traceback

os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

# ── 防重复导入守卫 ──────────────────────────────────────────────────────────
# 若本文件已被以另一模块名加载 (如顶层 `main` 与 `data.plugins.X.main` 并存),
# 再次执行模块体会重复注册全部指令 → WebUI 指令冲突 (cmd_blogin 等).
# 检测到重复时直接拒绝, 只保留首次加载的注册集.
_PL_THIS_FILE = os.path.abspath(__file__)
_PL_DUPLICATE_IMPORTS = [
    m.__name__
    for m in list(sys.modules.values())
    if m is not sys.modules.get(__name__)
    and getattr(m, "__file__", None)
    and os.path.abspath(m.__file__) == _PL_THIS_FILE
]
if _PL_DUPLICATE_IMPORTS:
    raise ImportError(
        "[ParserLite] main.py 已被重复加载: 首次加载于 "
        f"{_PL_DUPLICATE_IMPORTS[0]!r}, 本次加载于 {__name__!r}. "
        "为防指令重复注册冲突, 拒绝二次注册. 请检查 AstrBot 插件目录是否存在多个副本. "
    )

# AstrBot 插件根目录 → src/ 加入 sys.path (上游包 nonebot_plugin_parser_lite 在里面)
_here = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_here, "src")
if os.path.isdir(_src):
    sys.path.insert(0, _src)
sys.path.insert(0, _here)

# 新版 standalone: 数据目录指向插件目录下 data/ (避免散落 cwd/.parser-lite)
os.environ.setdefault("PARSER_LITE_BASE_DIR", os.path.join(_here, "data"))

# 清除上游模块缓存 — 多插件目录并存时防止从旧目录加载过期模块
for _mod in list(sys.modules):
    if _mod.startswith("nonebot_plugin_parser_lite"):
        del sys.modules[_mod]

from astrbot.api import AstrBotConfig
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# ── bridge core (拆分) ─────────────────────────────────────────────────────
from bridge.core import (  # noqa: F401
    BridgeConfig,
    ParserLite,
    _detect_missing_libs,
    _load_disabled_groups,
    configure,
    get_config,
)

_CONF_SCHEMA_PATH = Path(__file__).parent / "_conf_schema.json"


# ── 兼容 re-export (从 bridge.core) — 保持外部 API / 测试稳定 ──
from bridge.core import (  # noqa: F401
    _apply_downloader_proxy,
    _get_cookies_for,
    _is_parser_enabled,
    _label,
    _load_parsers_config,
    _try_load,
)

# ── Monkey-patch ────────────────────────────────────────────────────────────────
if not hasattr(logging.Logger, "success"):
    logging.Logger.success = logging.Logger.info

# ── 日志桥接 ──────────────────────────────────────────────────────────────────
_LOGBRIDGE_REENTRANT = __import__("threading").local()


class _LoguruBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # P1-5: 重入防护 — 桥接目标 logger 再进本 handler 时直接回退标准
        # logging (避免 opt(depth) 无法抵消嵌套导致无限递归/栈溢出)
        if getattr(_LOGBRIDGE_REENTRANT, "busy", False):
            try:
                logging.getLogger("parser-lite.bridge").log(
                    record.levelno, record.getMessage()
                )
            except Exception:
                pass
            return
        try:
            msg = self.format(record)
            lv = record.levelno
            if hasattr(astrbot_logger, "opt"):
                _LOGBRIDGE_REENTRANT.busy = True
                try:
                    fn = (
                        astrbot_logger.opt(depth=1).critical
                        if lv >= logging.CRITICAL
                        else astrbot_logger.opt(depth=1).error
                        if lv >= logging.ERROR
                        else astrbot_logger.opt(depth=1).warning
                        if lv >= logging.WARNING
                        else astrbot_logger.opt(depth=1).info
                        if lv >= logging.INFO
                        else astrbot_logger.opt(depth=1).debug
                    )
                    fn(msg)
                finally:
                    _LOGBRIDGE_REENTRANT.busy = False
            else:
                # 无 opt() 时回退标准 logging (避免 astrbot_logger.log 递归触发 emit)
                _std = logging.getLogger("parser-lite.bridge")
                _std.log(lv, msg)
        except Exception:
            pass


# ── 上游 imports ───────────────────────────────────────────────────────────────
# ── 动态注入 ──────────────────────────────────────────────────────────────────
# 模块加载时执行注入 (含 _injected 开关保护) — 委托 bridge.inject 决策树
from bridge.config import inject_dynamic_options_static  # noqa: E402
from bridge.format import format_full
from nonebot_plugin_parser_lite.data import (
    ParseResult,
)

inject_dynamic_options_static(
    Path(__file__).parent / "_conf_schema.json",
    Path(__file__).parent / ".injected",
)


# ── 格式化 ────────────────────────────────────────────────────────────────────
class ParserLitePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._log_bridge: _LoguruBridge | None = None
        self._parser: ParserLite | None = None
        self._chromium_task: asyncio.Task[None] | None = None
        self._plugin_start_time: float = time.time()
        self._disabled_groups: set[str] = set()

    async def initialize(self) -> None:
        try:
            # 上游 render 兼容补丁: 由 context.up_renderer() 首次渲染时自动应用 (收敛一处)
            self._log_bridge = _LoguruBridge()
            self._log_bridge.setFormatter(logging.Formatter("%(name)s | %(message)s"))
            sdk = logging.getLogger("nonebot_plugin_parser_lite")
            sdk.addHandler(self._log_bridge)
            sdk.setLevel(logging.DEBUG)
            astrbot_logger.info("[ParserLite]   日志桥接: OK")

            self._disabled_groups = _load_disabled_groups()

            configure(**self.config)
            cfg = get_config()
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                Path(cfg.data_dir) / "playwright_browsers"
            )

            for d in (cfg.cache_dir, cfg.config_dir, cfg.data_dir):
                await d.mkdir(parents=True, exist_ok=True)
            astrbot_logger.info("[ParserLite]   configure: OK")

            self._parser = ParserLite()
            self._plugin_start_time = time.time()
            # r8: push/limiter/debouncer 已移除 (自研业务模块删除);
            # 缓存清理由上游 pipeline scheduler 承载 (每 2h)
            self._chromium_task = asyncio.create_task(self._auto_ensure_chromium())
            astrbot_logger.info("[ParserLite] ✓ initialize 完成")
        except Exception:
            astrbot_logger.error(
                f"[ParserLite] ✗ initialize 失败\n{traceback.format_exc()}"
            )

    async def terminate(self) -> None:
        for _task_attr in ("_chromium_task",):
            _task = getattr(self, _task_attr, None)
            if _task:
                _task.cancel()
                try:
                    await _task
                except asyncio.CancelledError:
                    pass
        if self._parser is not None:
            try:
                await self._parser.close()
            except Exception:
                pass
        # r8: pusher 已移除; 上游运行时关闭由 ParserLite.close → shutdown_runtime 承载
        try:
            from nonebot_plugin_parser_lite.pipeline import shutdown_runtime

            await shutdown_runtime()
        except Exception:
            pass
        if self._log_bridge:
            try:
                logging.getLogger("nonebot_plugin_parser_lite").removeHandler(
                    self._log_bridge
                )
            except Exception:
                pass

    async def _auto_ensure_chromium(self) -> None:
        # r11: Chromium 安装统一编排 (bridge.browser, 与命令共用)
        from bridge.browser import ensure_chromium

        await ensure_chromium(
            browsers_path="",  # initialize 已设置 PLAYWRIGHT_BROWSERS_PATH
            log=astrbot_logger,
        )

    async def _parse_raw(self, url: str) -> ParseResult | None:
        if self._parser is None:
            return None
        # r8: 缓存由上游 pipeline._RESULT_CACHE 承载 (委托后无需自研缓存)
        try:
            return await self._parser.parse_url(url)
        except ValueError:
            return None
        except Exception:
            astrbot_logger.error(
                f"[ParserLite] _parse_raw 异常\n{traceback.format_exc()}"
            )
            return None

    async def _parse_and_format(self, url: str) -> str:
        result = await self._parse_raw(url)
        return format_full(result) if result else ""

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message_group(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_message_private(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    async def on_message(self, event: AstrMessageEvent):
        # r8: card_semantic 注入已移除 (自研模块删除)
        await self._handle_card_message(event)

    async def _handle_card_message(self, event: AstrMessageEvent):
        # r8: 消息级去重/限频/防抖已移除 (自研模块删除); 缓存由上游承载
        urls = self._extract_urls(event)
        if not urls:
            urls = self._reply_urls(event)  # 引用消息逃生通道 (小程序卡片)
        if not urls:
            return
        from bridge.commands import is_blacklisted, is_disabled
        from bridge.send import dispatch_result

        if is_disabled(self, event) or is_blacklisted(self, event):
            return

        for url in urls[:3]:
            try:
                result = await self._parse_raw(url)
                if result is None:
                    continue
                await dispatch_result(event, result)
            except Exception:
                astrbot_logger.error(
                    f"[ParserLite] _handle_card_message 异常\n{traceback.format_exc()}"
                )

    # ── 命令 (r11: 业务全委托 bridge.commands, 本层仅薄转发) ──────────────────
    async def cmd_parse(self, event: AstrMessageEvent):
        from bridge.commands import parse

        async for msg in parse(self, event):
            yield msg

    async def cmd_clean(self, event: AstrMessageEvent):
        from bridge.commands import clean_cache

        yield event.plain_result(clean_cache(self))

    async def cmd_status(self, event: AstrMessageEvent):
        from bridge.commands import status_text

        yield event.plain_result(status_text(self))

    async def cmd_enable(self, event: AstrMessageEvent):
        from bridge.commands import gid, toggle_group

        yield event.plain_result(toggle_group(self, gid(event), True))

    async def cmd_disable(self, event: AstrMessageEvent):
        from bridge.commands import gid, toggle_group

        yield event.plain_result(toggle_group(self, gid(event), False))

    async def cmd_doctor(self, event: AstrMessageEvent):
        from bridge.commands import doctor

        async for msg in doctor(self, event):
            yield msg

    async def cmd_install_chromium(self, event: AstrMessageEvent):
        from bridge.commands import install_chromium

        async for msg in install_chromium(self, event):
            yield msg

    async def cmd_bm(self, event: AstrMessageEvent):
        from bridge.commands import bm

        async for msg in bm(self, event):
            yield msg

    async def cmd_blogin(self, event: AstrMessageEvent):
        from bridge.commands import blogin

        async for msg in blogin(self, event):
            yield msg

    async def parse_url(self, event: AstrMessageEvent, url: str) -> str:
        from bridge.commands import parse_url_cmd as _parse_url

        return await _parse_url(self, event, url)


# ── 装饰器注册 ────────────────────────────────────────────────────────────────
filter.command("parse")(ParserLitePlugin.cmd_parse)
filter.command("parse_clean")(ParserLitePlugin.cmd_clean)
filter.command("parse_status")(ParserLitePlugin.cmd_status)
filter.command("parse_enable")(ParserLitePlugin.cmd_enable)
filter.command("parse_disable")(ParserLitePlugin.cmd_disable)
filter.command("parse_doctor")(ParserLitePlugin.cmd_doctor)
filter.command("parser_doctor")(
    ParserLitePlugin.cmd_doctor
)  # 别名 (用户习惯 /parser_doctor)
filter.command("parse_install_chromium")(ParserLitePlugin.cmd_install_chromium)
filter.command("cmd_bm")(ParserLitePlugin.cmd_bm)
filter.command("cmd_blogin")(ParserLitePlugin.cmd_blogin)
filter.llm_tool(name="parse_url")(ParserLitePlugin.parse_url)
