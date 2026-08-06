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
import re
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
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

# ── bridge core (拆分) ─────────────────────────────────────────────────────
from bridge.core import (
    BridgeConfig,
    LazyManager,
    ParserLite,
    _detect_missing_libs,
    _load_disabled_groups,
    configure,
    get_config,
)

_CONF_SCHEMA_PATH = Path(__file__).parent / "_conf_schema.json"


# ── 兼容 re-export (从 bridge.core) — 保持外部 API / 测试稳定 ──
from bridge.core import (  # noqa: F401
    _PROXY_PROTOCOLS,
    _apply_downloader_proxy,
    _get_cookies_for,
    _is_parser_enabled,
    _label,
    _load_parsers_config,
    _read_proxy_config,
    _resolve_proxy_url,
    _try_load,
    _use_proxy_for,
)

# ── Monkey-patch ────────────────────────────────────────────────────────────────
if not hasattr(logging.Logger, "success"):
    logging.Logger.success = logging.Logger.info

# ── 日志桥接 ──────────────────────────────────────────────────────────────────
class _LoguruBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            lv = record.levelno
            if hasattr(astrbot_logger, "opt"):
                fn = (
                    astrbot_logger.opt(depth=1).critical if lv >= logging.CRITICAL
                    else astrbot_logger.opt(depth=1).error if lv >= logging.ERROR
                    else astrbot_logger.opt(depth=1).warning if lv >= logging.WARNING
                    else astrbot_logger.opt(depth=1).info if lv >= logging.INFO
                    else astrbot_logger.opt(depth=1).debug
                )
                fn(msg)
            else:
                # 无 opt() 时回退标准 logging (避免 astrbot_logger.log 递归触发 emit)
                _std = logging.getLogger("parser-lite.bridge")
                _std.log(lv, msg)
        except Exception:
            pass

# ── 上游 imports ───────────────────────────────────────────────────────────────
from nonebot_plugin_parser_lite.data import (
    AudioContent,
    GraphicContent,
    ImageContent,
    ParseResult,
    StickerContent,
    VideoContent,
)
from nonebot_plugin_parser_lite.parsers.base import BaseParser
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.common import LimitedSizeDict

CACHE_INTERVAL = 24 * 3600
_RESULT_CACHE: LimitedSizeDict[str, ParseResult] = LimitedSizeDict(max_size=50)
_CARD_CACHE: dict[str, bytes] = {}
_CARD_CACHE_MAX = 20  # LRU 上限 (动态可调)
from bridge.format import format_full


def _bridge_cfg(key: str, default=None):
    """读取 bridge 配置 (统一入口, 缺失回退默认值)."""
    from bridge.cfg import read_cfg

    return read_cfg(BridgeConfig._source, key, default)


# ── 动态注入 ──────────────────────────────────────────────────────────────────
# 模块加载时执行注入 (含 _injected 开关保护) — 委托 bridge.inject 决策树
from bridge.inject import inject_dynamic_options_static  # noqa: E402
from bridge.url_extract import (
    collect_urls,
    extract_card_json_url,
    extract_urls,
    url_from_text,
)
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

inject_dynamic_options_static(
    Path(__file__).parent / "_conf_schema.json",
    Path(__file__).parent / ".injected",
)

# ── 格式化 ────────────────────────────────────────────────────────────────────
# ── 格式化 (已移至 bridge.format) ──
# ── 懒下载管理器 ──────────────────────────────────────────────────────────────
class ParserLitePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._log_bridge: _LoguruBridge | None = None
        self._parser: ParserLite | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._chromium_task: asyncio.Task[None] | None = None
        self._plugin_start_time: float = time.time()
        self._disabled_groups: set[str] = set()
        self._recently_processed: dict[int, float] = {}
        self._limiter = None  # 延迟到 initialize 创建 (需要 base_dir)
        self._debouncer = None  # 链接级防抖 (E5)
        self._delay_sender = None  # 延迟发送 (F7)

    async def initialize(self) -> None:
        try:
            # 上游 render 兼容补丁: safe_src 默认 method (模板省略调用)
            try:
                from bridge.render_patch import apply_render_patch
                apply_render_patch()
            except Exception:
                pass
            self._log_bridge = _LoguruBridge()
            self._log_bridge.setFormatter(logging.Formatter("%(name)s | %(message)s"))
            sdk = logging.getLogger("nonebot_plugin_parser_lite")
            sdk.addHandler(self._log_bridge)
            sdk.setLevel(logging.DEBUG)
            astrbot_logger.info("[ParserLite]   日志桥接: OK")

            self._disabled_groups = _load_disabled_groups()

            configure(**self.config)
            cfg = get_config()
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(cfg.data_dir) / "playwright_browsers")

            for d in (cfg.cache_dir, cfg.config_dir, cfg.data_dir):
                await d.mkdir(parents=True, exist_ok=True)
            astrbot_logger.info("[ParserLite]   configure: OK")

            self._parser = ParserLite()
            self._plugin_start_time = time.time()
            # 频率限制器 + 防抖器 (配置驱动, 统一状态目录持久化)
            try:
                from bridge.debounce import make_debouncer
                from bridge.paths import state_dir as _state_dir
                from bridge.rate_limit import make_limiter
                self._limiter = make_limiter(_state_dir())
                self._debouncer = make_debouncer(_state_dir())
            except Exception:
                self._limiter = None
                self._debouncer = None
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._chromium_task = asyncio.create_task(self._auto_ensure_chromium())
            # F1: B站 UP 订阅推送 (配置驱动, 默认关闭; push 为可增删 template_list)
            self._pusher = None
            try:
                from bridge.push import load_cfg as _push_cfg
                from bridge.push import make_pusher
                _push_raw, _interval = _push_cfg()
                from bridge.paths import state_dir as _state_dir
                self._pusher = make_pusher(_state_dir())
                # template_list: [{uid, groups("1,2"), enabled}]
                _subs: dict[str, list[str]] = {}
                if isinstance(_push_raw, list):
                    for entry in _push_raw:
                        if not isinstance(entry, dict):
                            continue
                        if not entry.get("enabled", True):
                            continue
                        _uid = str(entry.get("uid", "") or "").strip()
                        _grp = str(entry.get("groups", "") or "").strip()
                        if _uid:
                            _subs[_uid] = [g.strip() for g in _grp.split(",") if g.strip()]
                elif isinstance(_push_raw, dict):
                    # 兼容旧格式 {uid: [groups]}
                    _subs = {str(k): [str(g) for g in v] for k, v in _push_raw.items()}
                self._pusher.set_subscriptions(_subs)
                async def _push_send(msg: str, groups: list[str]):
                    for gid in groups:
                        try:
                            await self.context.send_message(
                                f"aiocqhttp:GroupMessage:{gid}", [Comp.Plain(msg)])
                        except Exception as _e:
                            astrbot_logger.warning(f"[ParserLite] 推送失败 {gid}: {_e}")

                if _subs:
                    self._pusher.start(_interval, _push_send)
                    astrbot_logger.info(f"[ParserLite] UP 推送已启动: {len(_subs)} 订阅, 间隔 {_interval}s")
            except Exception as _e:
                astrbot_logger.warning(f"[ParserLite] 推送初始化跳过: {_e}")
                self._pusher = None
            # F4: Cookie 健康检查 (配置驱动, 默认关闭)
            self._cookie_health = None
            try:
                from bridge.cookie_health import load_cfg as _ch_cfg
                from bridge.cookie_health import make_cookie_health
                from bridge.paths import state_dir as _state_dir
                _ck_cfg = _ch_cfg()
                self._cookie_health = make_cookie_health(_state_dir())
                _ck_interval = float(_ck_cfg.get("interval_sec", 3600) or 3600)
                _cookies = {
                    "bilibili": _bridge_cfg("plite_bili_ck", "") or "",
                    "zhihu": _bridge_cfg("plite_zhihu_ck", "") or "",
                }

                async def _ck_notify(msg: str):
                    astrbot_logger.warning(msg)
                    try:
                        await self.context.send_message(
                            "aiocqhttp:GroupMessage:0", [Comp.Plain(msg)])
                    except Exception:
                        pass

                if _ck_cfg.get("enabled", False):
                    self._cookie_health.start(_ck_interval, _cookies, _ck_notify)
                    astrbot_logger.info(f"[ParserLite] Cookie 健康检查已启动: {_ck_interval}s")
            except Exception as _e:
                astrbot_logger.warning(f"[ParserLite] Cookie 检查初始化跳过: {_e}")
                self._cookie_health = None
            # F7: 延迟发送器 (表情触发, 配置驱动)
            self._delay_sender = None
            try:
                from bridge.delay_send import make_delay_sender
                self._delay_sender = make_delay_sender()
            except Exception:
                self._delay_sender = None
            astrbot_logger.info("[ParserLite] ✓ initialize 完成")
        except Exception:
            astrbot_logger.error(f"[ParserLite] ✗ initialize 失败\n{traceback.format_exc()}")

    async def terminate(self) -> None:
        for _task_attr in ("_cleanup_task", "_chromium_task"):
            _task = getattr(self, _task_attr, None)
            if _task:
                _task.cancel()
                try: await _task
                except asyncio.CancelledError: pass
        if self._parser is not None:
            try: await self._parser.close()
            except Exception: pass
        # F1: 停止 UP 推送轮询
        if self._pusher is not None:
            try: await self._pusher.stop()
            except Exception: pass
        # F4: 停止 cookie 健康检查
        if self._cookie_health is not None:
            try: await self._cookie_health.stop()
            except Exception: pass
        # 新版 standalone 运行时: 关闭 scheduler + BrowserManager + DOWNLOADER
        try:
            from nonebot_plugin_parser_lite.pipeline import shutdown_runtime
            await shutdown_runtime()
        except Exception:
            pass
        if self._log_bridge:
            try:
                logging.getLogger("nonebot_plugin_parser_lite").removeHandler(self._log_bridge)
            except Exception: pass

    async def _cleanup_loop(self) -> None:
        while True:
            _interval = float(_bridge_cfg("plite_cache_interval", CACHE_INTERVAL) or 3600)
            await asyncio.sleep(_interval)
            await self._do_clean_cache()

    async def _do_clean_cache(self) -> int:
        try:
            count = await CacheManager.clean_expired()
            if count: astrbot_logger.info(f"[ParserLite] 缓存清理: {count} files")
            return count
        except Exception:
            astrbot_logger.error(f"[ParserLite] 缓存清理异常\n{traceback.format_exc()}")
            return 0

    async def _auto_ensure_chromium(self) -> None:
        try:
            # 新版: 通过 BrowserManager 验证 (复用上游单例, 与 render 共用)
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            astrbot_logger.info("[ParserLite] Chromium 已就绪"); return
        except Exception: pass
        astrbot_logger.info("[ParserLite] Chromium 未安装, 异步安装中...")
        pb = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        installed = False
        for url, name in [("https://npmmirror.com/mirrors/playwright","npmmirror"),
                          ("https://playwright.azureedge.net","Azure")]:
            env = os.environ.copy(); env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
            if pb: env["PLAYWRIGHT_BROWSERS_PATH"] = pb
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium",
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                if proc.returncode == 0:
                    astrbot_logger.info(f"[ParserLite] Chromium 安装完成 ({name})")
                    installed = True
                    break
                err = stderr.decode(errors="replace").strip()[-300:]
                astrbot_logger.warning(f"[ParserLite] Chromium 安装失败 ({name}): rc={proc.returncode} {err}")
            except asyncio.TimeoutError:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装超时 ({name})")
            except Exception as e:
                astrbot_logger.warning(f"[ParserLite] Chromium 安装异常 ({name}): {e}")
        if not installed:
            # 浏览器二进制已下载但缺系统库 → 尝试 apt-get 自动补齐
            missing = _detect_missing_libs()
            if missing:
                astrbot_logger.warning(f"[ParserLite] 检测到缺失系统库, 尝试 apt-get 安装:\n{missing}")
                try:
                    _apt_proc = await asyncio.create_subprocess_exec(
                        "apt-get", "update",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await asyncio.wait_for(_apt_proc.communicate(), timeout=300)
                    _apt_proc = await asyncio.create_subprocess_exec(
                        "apt-get", "install", "-y", "--no-install-recommends",
                        "libnspr4", "libnss3", "libgbm1", "libasound2", "libxkbcommon0",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    _out, _err = await asyncio.wait_for(_apt_proc.communicate(), timeout=600)
                    if _apt_proc.returncode == 0:
                        astrbot_logger.info("[ParserLite] 系统库安装完成, 验证 Chromium...")
                        try:
                            from nonebot_plugin_parser_lite.utils.browser import (
                                BrowserManager,
                            )
                            await BrowserManager.ensure_started()
                            astrbot_logger.info("[ParserLite] Chromium 已就绪 (系统库补齐后)")
                            return
                        except Exception as _e2:
                            astrbot_logger.error(
                                f"[ParserLite] ✗ 系统库已安装但 Chromium 仍无法启动: {_e2}")
                    else:
                        astrbot_logger.error(
                            f"[ParserLite] ✗ apt-get 安装系统库失败: rc={_apt_proc.returncode} "
                            f"{_err.decode(errors='replace').strip()[-300:]}")
                except asyncio.TimeoutError:
                    astrbot_logger.error("[ParserLite] ✗ apt-get 安装系统库超时")
                except Exception as _e3:
                    astrbot_logger.error(f"[ParserLite] ✗ apt-get 异常: {_e3}")
            # 显式报错: 列出缺失库 + 修复指引
            _missing_now = _detect_missing_libs()
            astrbot_logger.error(
                "[ParserLite] ✗✗ Chromium 环境自动组装失败, 卡片渲染将回退为文本 ✗✗\n"
                f"缺失系统库:\n{_missing_now or '(未检测到缺失库, 请检查 playwright 安装)'}\n"
                "修复方式(需容器 root):\n"
                "  1) apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0\n"
                "  2) 或运行: python -m playwright install-deps chromium\n"
                "  3) 或发送指令 /parse_install_chromium 重试浏览器下载")
            return
        # 二进制就绪后仍验证启动 (apt 补库可能仍失败)
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            astrbot_logger.info("[ParserLite] Chromium 已就绪")
        except Exception as _e:
            _missing_after = _detect_missing_libs()
            astrbot_logger.error(
                f"[ParserLite] ✗ Chromium 已下载但无法启动: {_e}\n"
                f"缺失系统库: {_missing_after or '(none detected)'}\n"
                "请运行: apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0"
                " 或 python -m playwright install-deps chromium")

    # ── OneBot 适配 ───────────────────────────────────────────────────────────
    def _gid(self, event: AstrMessageEvent) -> str:
        try:
            o = event.unified_msg_origin
            return o.split(":")[-1] if o and ":" in o else "unknown"
        except Exception: return "unknown"

    def _key(self, event: AstrMessageEvent) -> str:
        try: return event.unified_msg_origin or event.get_sender_id()
        except Exception: return event.get_sender_id()

    def _disabled(self, event: AstrMessageEvent) -> bool:
        return self._gid(event) in self._disabled_groups

    def _blacklisted(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in get_config().blacklist_users

    def _clean_lazy(self) -> int:
        return LazyManager.cleanup()

    @staticmethod
    def _url_from_text(event: AstrMessageEvent) -> str | None:
        return url_from_text(event.get_message_str)

    @classmethod
    def _extract_urls(cls, event: AstrMessageEvent) -> list[str]:
        import astrbot.api.message_components as _Comp
        return extract_urls(event, _Comp)

    @staticmethod
    def _reply_urls(event: AstrMessageEvent) -> list[str]:
        """从被回复消息中提取 URL — 小程序卡片链接的逃生通道.

        兼容 OneBot reply 段 (data.text / data.message) 与 AstrBot message_obj 链.
        """
        urls: list[str] = []
        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or []
        for seg in chain if isinstance(chain, list) else []:
            seg_type = str(seg.get("type", "")) if isinstance(seg, dict) else ""
            if "reply" not in seg_type:
                continue
            data = seg.get("data", {}) if isinstance(seg, dict) else {}
            if not isinstance(data, dict):
                continue
            for key in ("text", "message", "content"):
                raw = data.get(key, "")
                if isinstance(raw, list):
                    for sub in raw:
                        if isinstance(sub, dict):
                            sub_data = sub.get("data", {})
                            if isinstance(sub_data, dict):
                                collect_urls(str(sub_data.get("text", "")), urls)
                                d = sub_data.get("data", "")
                                if isinstance(d, str) and d:
                                    collect_urls(d, urls)
                                    u = extract_card_json_url(d)
                                    if u:
                                        urls.append(u)
                elif isinstance(raw, str) and raw:
                    collect_urls(raw, urls)
                    u = extract_card_json_url(raw)
                    if u:
                        urls.append(u)
        # 去重
        seen = set()
        result = []
        for u in urls:
            u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

    async def _parse_raw(self, url: str) -> ParseResult | None:
        if self._parser is None: return None
        if url in _RESULT_CACHE: return _RESULT_CACHE[url]
        try:
            result = await self._parser.parse_url(url)
            _RESULT_CACHE[url] = result
            return result
        except ValueError: return None
        except Exception:
            astrbot_logger.error(f"[ParserLite] _parse_raw 异常\n{traceback.format_exc()}")
            return None

    async def _parse_and_format(self, url: str) -> str:
        result = await self._parse_raw(url)
        return format_full(result) if result else ""

    # ── 多媒体发送管线 (委托 bridge.send: OneBot11 分派 + FFmpeg 转换回调注入) ──
    async def _send_any(self, event: AstrMessageEvent, p: Path, media_type: str,
                        source_url: str = "", duration: float = 0.0, cover_path: str = ""):
        from bridge.send import send_media_file

        converters = {
            "image": self._compress_image,
            "video": self._convert_video,
            "audio": lambda path: self._convert_audio(path, fmt="mp3"),
        }
        report = await send_media_file(event, p, media_type, source_url, converters,
                                       astrbot_logger, cover_path=cover_path)
        if report:
            return
        # 发送失败回显 (OneBot11 发送反馈: 可倒查发送功能缺陷)
        try:
            _why = "; ".join(report.errors[-3:]) or "OneBot API 不可用"
            await event.send(event.chain_result([
                Comp.Plain(f"[ParserLite] {media_type} 发送失败: {_why}")]))
        except Exception:
            pass
        # delay_send 兜底: 大视频三路失败 → 表情触发延迟发送 (扩展逻辑保留 main)
        if media_type == "video" and p.exists():
            from bridge.delay_send import load_cfg as _dl_cfg_fn
            _dl_cfg = _dl_cfg_fn()
            if _dl_cfg.get("enabled", False) and self._delay_sender is not None:
                _threshold = int(_dl_cfg.get("threshold_mb", 20) or 20) * 1024 * 1024
                _msg_id = getattr(getattr(event, "message_obj", None), "raw_message", None)
                _msg_id = (_msg_id or {}).get("message_id") if isinstance(_msg_id, dict) else None
                _sz = p.stat().st_size if p.exists() else 0
                if _msg_id and _sz > _threshold:
                    _dl_key = f"{_msg_id}:{p.name}"
                    self._delay_sender.arm(str(_msg_id), _dl_key,
                                           timeout_sec=float(_dl_cfg.get("timeout_sec", 300) or 300))

                    async def _do_delay_send(_key):
                        try:
                            await self._send_any(event, p, "video",
                                                source_url=source_url, duration=duration)
                        except Exception:
                            pass
                    self._delay_sender.set_trigger(_do_delay_send)
                    try:
                        await event.send(event.chain_result([Comp.Plain(
                            f"视频较大 ({_sz // 1024 // 1024}MB), 回应 👍 后发送")]))
                    except Exception:
                        pass

    async def _compress_image(self, p: Path) -> bytes:
        """压缩超大图片 (JPEG quality 80%, 最大 20MB)"""
        import io

        from PIL import Image as PILImage
        img = PILImage.open(str(p))
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
        return buf.getvalue()

    async def _convert_audio(self, p: Path, fmt: str = "mp3") -> Path:
        """FFmpeg 音频转码: → MP3 (128k) 或 AMR (8k mono)"""
        if not await FFmpeg.is_available():
            return p
        if p.suffix.lower() in (".mp3", ".m4a", ".aac", ".wav") and fmt == "mp3":
            return p
        out = p.parent / f"{p.stem}_cvt.{fmt}"
        if out.exists(): return out
        opts = ["-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                "-ac", "1", "-ar", "44100", "-b:a", "128k", str(out)] if fmt == "mp3" else \
               ["-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                "-ac", "1", "-ar", "8000", "-b:a", "12.2k", str(out)]
        try:
            await FFmpeg.exec_ffmpeg(opts)
            return out
        except Exception:
            return p

    async def _convert_video(self, p: Path) -> Path:
        """FFmpeg 视频转封装/转码: → H.264 + AAC in MP4"""
        if not await FFmpeg.is_available():
            return p
        if p.suffix.lower() == ".mp4":
            return p
        out = p.parent / f"{p.stem}_cvt.mp4"
        if out.exists(): return out
        try:
            await FFmpeg.exec_ffmpeg([
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(p), "-c:v", "libx264", "-preset", "fast",
                "-crf", "28", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(out),
            ])
            return out
        except Exception:
            return p

    async def _send_card(self, event: AstrMessageEvent, result: ParseResult):
        # 委托 bridge.send (薄发送层: 上游渲染 → AstrBot 发送, 文本回退)
        # OneBot11 发送反馈: 失败时向用户回显原因 (可倒查发送功能缺陷)
        from bridge.send import send_card

        report = await send_card(event, result, format_full, astrbot_logger)
        if not report:
            _why = "; ".join(report.errors[-2:]) or "未知原因"
            try:
                await event.send(event.chain_result([
                    Comp.Plain(f"[ParserLite] 解析成功但卡片发送失败: {_why}")]))
            except Exception:
                pass
        return report

    # ── 自动触发的 URL 解析 ────────────────────────────────────────────────────
    async def on_url_auto(self, event: AstrMessageEvent):
        # on_message_group/on_message_private 与 regex filter 会先后触发同一条消息,
        # 导致同一 URL 被解析两次 (dedup 使用 message_id, 两个事件 id 可能不同)
        return  # 避免重复 — 群聊/私聊由 on_message_group/on_message_private 覆盖

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message_group(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_message_private(self, event: AstrMessageEvent):
        await self._handle_card_message(event)

    async def on_message(self, event: AstrMessageEvent):
        # E6: notice 事件 (表情回应) 分流到仲裁器
        try:
            from bridge.arbiter import is_notice_event
            if is_notice_event(event):
                await self.on_notice(event)
                return
        except Exception:
            pass
        # F2: QQ 卡片 → LLM 结构化文本注入 (配置驱动, 默认开)
        try:
            from bridge.card_semantic import find_json_cards, inject_card_summary
            if _bridge_cfg("card_semantic", True):
                for _entry in find_json_cards(event)[:2]:
                    inject_card_summary(event, _entry["card"])
        except Exception:
            pass
        await self._handle_card_message(event)

    def _should_send(self, media_type: str) -> bool:
        """发送策略门: 委托 bridge.send (配置驱动, 默认全部类型)."""
        from bridge.send import should_send

        return should_send(media_type)

    async def _send_items(self, event: AstrMessageEvent, items: list, result: ParseResult):
        """统一发送入口: 超过4项且配置允许 → 合并转发, 否则逐一发送"""
        need_forward = (
            get_config().need_forward_contents
            and len([i for i in items if hasattr(i, "path_task")]) > 4
        )
        if need_forward:
            await self._send_as_forward(event, items, result)
        else:
            for item in items:
                await self._send_one(event, item)

    async def _send_one(self, event: AstrMessageEvent, item):
        """发送单个媒体项"""
        if not hasattr(item, "path_task"): return
        try:
            src_url = getattr(item.path_task, "url", "")
            dur = getattr(item, "duration", 0.0)
            # bridge 语义字段从 _source 读取 (不在上游 Config 模型)
            _direct = bool(_bridge_cfg("plite_direct_link", False))
            _cover_only = bool(_bridge_cfg("plite_send_cover_only", False))
            # F5: 直链免下载模式 (配置驱动, 非硬编码)
            if _direct and src_url:
                sent = await self._try_direct_send(event, item, src_url)
                if sent:
                    return
            # F6: 视频仅发封面 (配置驱动)
            if isinstance(item, VideoContent) and _cover_only:
                if self._should_send("image"):
                    await self._send_video_cover(event, item)
                return
            p = Path(str(await item.path_task))
            # 视频封面: 优先上游 VideoContent.cover (原始调用), 无则 ffmpeg 截帧兜底
            _cover_p = ""
            if isinstance(item, VideoContent):
                _cover_p = str(await self._resolve_cover_path(item)) or ""
            if isinstance(item, (ImageContent, GraphicContent, StickerContent)):
                if self._should_send("image"):
                    await self._send_any(event, p, "image", source_url=src_url)
            elif isinstance(item, VideoContent):
                if self._should_send("video"):
                    await self._send_any(event, p, "video", source_url=src_url,
                                         duration=dur, cover_path=_cover_p)
            elif isinstance(item, AudioContent):
                if self._should_send("audio"):
                    await self._send_any(event, p, "audio", source_url=src_url, duration=dur)
        except Exception: pass

    async def _resolve_cover_path(self, item) -> Path | None:
        """视频封面路径: 优先上游 cover path_task (原始调用), 失败返回 None."""
        try:
            _cover = getattr(item, "cover", None)
            if _cover is not None and getattr(_cover, "path_task", None) is not None:
                _cp = Path(str(await _cover.path_task))
                if _cp.exists():
                    return _cp
        except Exception:
            pass
        return None

    async def _try_direct_send(self, event: AstrMessageEvent, item, src_url: str) -> bool:
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
            max_mb = int(get_config().max_size)
            if size > max_mb * 1024 * 1024:
                return False  # 超限回退下载
            if isinstance(item, VideoContent):
                if self._should_send("video"):
                    await event.send(event.chain_result(
                        [Comp.Video.fromURL(src_url)]))
                return True
            if isinstance(item, (ImageContent, GraphicContent)):
                if self._should_send("image"):
                    await event.send(event.chain_result(
                        [Comp.Image.fromURL(src_url)]))
                return True
            return False
        except Exception:
            return False

    async def _send_video_cover(self, event: AstrMessageEvent, item) -> None:
        """F6: 视频仅发封面 — 优先上游 cover (原始调用), 无则 ffmpeg 截帧兜底."""
        try:
            _cp = await self._resolve_cover_path(item)
            if _cp is not None:
                if self._should_send("image"):
                    await self._send_any(event, _cp, "image",
                                         source_url=getattr(item.path_task, "url", ""))
                return
            from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
            if not await FFmpeg.is_available():
                return
            vpath = Path(str(await item.path_task))
            cover = vpath.parent / f"{vpath.stem}_cover.jpg"
            await FFmpeg.exec_ffmpeg([
                "-i", str(vpath), "-frames:v", "1", "-q:v", "5",
                "-y", str(cover),
            ])
            if cover.exists():
                if self._should_send("image"):
                    await self._send_any(event, cover, "image",
                                         source_url=getattr(item.path_task, "url", ""))
                cover.unlink(missing_ok=True)
        except Exception:
            pass

    async def _send_as_forward(self, event: AstrMessageEvent, items: list, result: ParseResult):
        """合并转发: 将多项媒体内容打包为 Comp.Nodes (移植自上游 Renderer.__build_forward_segs)"""
        nodes = []
        author = result.author.name if result.author and result.author.name else "解析"
        platform = result.platform.display_name if result.platform else ""
        MAX_PER_NODE = int(_bridge_cfg("plite_forward_max_nodes", 90) or 90)

        for item in items:
            if not hasattr(item, "path_task"): continue
            if len(nodes) >= MAX_PER_NODE: break
            try:
                p = Path(str(await item.path_task))
                if isinstance(item, ImageContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} | {platform}"),
                                 Comp.Image.fromFileSystem(str(p))],
                        name=author, uin="0"))
                elif isinstance(item, VideoContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} 的视频"),
                                 Comp.Video.fromFileSystem(str(p))],
                        name=author, uin="0"))
                elif isinstance(item, AudioContent):
                    nodes.append(Comp.Node(
                        content=[Comp.Plain(f"{author} 的音频"),
                                 Comp.Record.fromFileSystem(str(p))],
                        name=author, uin="0"))
            except Exception: pass

        if nodes:
            # E4: 发送降级链 — 合并转发失败 → 逐项单发 (动态降级, 无硬编码)
            from bridge.fallback import send_with_fallback

            async def _try_forward() -> bool:
                await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))
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
                logger=astrbot_logger,
                label="合并转发",
            )

    async def on_notice(self, event: AstrMessageEvent):
        """E6: 多 Bot 表情仲裁 + F7: 延迟发送触发 — 处理 group_msg_emoji_like notice.

        AstrBot 将 OneBot notice 事件转为 AstrMessageEvent (raw_message 保留原始 dict).
        """
        try:
            from bridge.arbiter import check_notice, parse_notice
            raw = getattr(event, "raw_message", None)
            if isinstance(raw, dict):
                parsed = parse_notice(raw)
                if parsed:
                    msg_id, emoji_id = parsed
                    # F7: 延迟发送触发 (先于仲裁, 互不冲突)
                    if self._delay_sender is not None:
                        from bridge.delay_send import load_cfg as _dl_cfg_fn
                        _dl_cfg = _dl_cfg_fn()
                        _want = [str(x) for x in (_dl_cfg.get("emoji_ids", []) or [])]
                        if self._delay_sender.on_emoji_like(msg_id, emoji_id, _want):
                            astrbot_logger.info(f"[ParserLite] 延迟发送触发: msg={msg_id}")
                            return
                    # E6: 仲裁
                    if check_notice(msg_id, emoji_id):
                        astrbot_logger.debug(f"[ParserLite] 仲裁: 其他 bot 已竞争 {msg_id}, 放弃")
        except Exception:
            pass

    async def _handle_card_message(self, event: AstrMessageEvent):
        # 二选一门: 用原始 message_id 去重 (跨 handler 实例, TTL=60s)
        msg_id = None
        # AstrBot aiocqhttp → event.message_obj.raw_message.message_id
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            raw = getattr(msg_obj, "raw_message", None)
            if isinstance(raw, dict):
                msg_id = raw.get("message_id")
        # fallback: event.get_message_str() 取 hash
        if msg_id is None:
            msg_id = hash(event.get_message_str())
        now = time.time()
        _dedup_ttl = float(_bridge_cfg("plite_dedup_ttl", 60) or 60)
        if msg_id in self._recently_processed:
            if now - self._recently_processed[msg_id] < _dedup_ttl:
                return
        self._recently_processed[msg_id] = now
        if len(self._recently_processed) > 50:
            cutoff = now - _dedup_ttl
            self._recently_processed = {k: v for k, v in self._recently_processed.items() if v > cutoff}
        # E6: 多 Bot 仲裁 — 武装竞争窗口 (参数动态, 默认关闭)
        from bridge.arbiter import load_cfg as _arb_cfg
        _arbiter_cfg = _arb_cfg()
        if _arbiter_cfg.get("enabled", False):
            try:
                from bridge.arbiter import arm
                _emoji = _arbiter_cfg.get("emoji", "") or None
                _win = _arbiter_cfg.get("window_sec", None)
                if not arm(str(msg_id), emoji=_emoji, window_sec=_win):
                    astrbot_logger.debug("[ParserLite] 仲裁: 已放弃此消息")
                    return
            except Exception:
                pass
        urls = self._extract_urls(event)
        if not urls:
            urls = self._reply_urls(event)  # 引用消息逃生通道 (小程序卡片)
        if not urls: return
        if self._disabled(event) or self._blacklisted(event): return
        # 频率限制 (配置驱动)
        if self._limiter is not None:
            from bridge.rate_limit import clean_url, load_rate_cfg
            _rcfg = load_rate_cfg(BridgeConfig._source)
            _sender = event.get_sender_id() or ""
            for _u in urls[:3]:
                _ok, _why = self._limiter.allow(url=clean_url(_u), user_id=str(_sender), cfg=_rcfg)
                if not _ok:
                    astrbot_logger.info(f"[ParserLite] 限频: {_why}")
                    try:
                        await event.send(event.chain_result([Comp.Plain(_why)]))
                    except Exception:
                        pass
                    return
        for url in urls[:3]:
            # E5: 链接级防抖 (持久化) — 窗口秒数动态从配置读取, 失败回滚
            if self._debouncer is not None:
                from bridge.debounce import debounce_key
                from bridge.rate_limit import clean_url
                _session = self._key(event)
                _dkey = debounce_key(_session, clean_url(url))
                # 防抖窗口: plite_dedup_ttl (自实现字段, 不误用上游 lazy_download_timeout)
                _dwin = float(_bridge_cfg("plite_dedup_ttl", 60) or 60)
                if not self._debouncer.should_parse(_dkey, _dwin):
                    continue  # 防抖命中
            try:
                result = await self._parse_raw(url)
                if result is None:
                    if self._debouncer is not None:
                        self._debouncer.rollback(_dkey)  # 失败回滚, 允许重试
                    continue
                if self._debouncer is not None:
                    self._debouncer.mark_success(_dkey)
                if self._should_send("card"):
                    await self._send_card(event, result)
                await self._send_items(event, result.content, result)
            except Exception:
                if self._debouncer is not None:
                    self._debouncer.rollback(_dkey)
                astrbot_logger.error(f"[ParserLite] _handle_card_message 异常\n{traceback.format_exc()}")

    # ── 命令 ──────────────────────────────────────────────────────────────────
    async def cmd_parse(self, event: AstrMessageEvent):
        if self._blacklisted(event) or self._disabled(event):
            yield event.plain_result("本群已禁用"); return
        urls = self._extract_urls(event)
        if not urls:
            urls = self._reply_urls(event)  # 引用消息逃生通道 (小程序卡片)
        if not urls:
            yield event.plain_result("未找到链接"); return
        url = urls[0]
        astrbot_logger.info(f"[ParserLite] cmd_parse: {url[:120]}")
        try:
            result = await self._parse_raw(url)
            if result is None:
                yield event.plain_result("不支持的链接"); return
            if self._should_send("card"):
                await self._send_card(event, result)
            await self._send_items(event, result.content, result)
            if result.platform and result.platform.name == "bilibili":
                LazyManager.add(self._key(event), result, result.url,
                                get_config().plite_lazy_download_timeout)
        except Exception as e:
            astrbot_logger.error(f"[ParserLite] cmd_parse 异常\n{traceback.format_exc()}")
            yield event.plain_result(f"解析失败: {e}")

    async def cmd_parse_dl(self, event: AstrMessageEvent):
        urls = self._extract_urls(event)
        if urls:
            async for _ in self.cmd_parse(event): yield _
        else:
            yield event.plain_result("未找到链接")

    async def _on_download_trigger(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        if not re.match(r"^(xz|下载)$", text): return
        key = self._key(event)
        session = LazyManager.get(key)
        if not session:
            yield event.plain_result("没有待下载的链接"); return
        LazyManager.remove(key)
        result = await self._parse_raw(session.url)
        if result is None:
            yield event.plain_result("不支持的链接"); return
        if self._should_send("card"):
            await self._send_card(event, result)
        await self._send_items(event, result.content, result)
        yield event.plain_result("已下载")

    async def cmd_clean(self, event: AstrMessageEvent):
        from bridge.commands import clean_cache

        yield event.plain_result(clean_cache(self))

    async def cmd_status(self, event: AstrMessageEvent):
        from bridge.commands import status_text

        yield event.plain_result(status_text(self))

    async def cmd_enable(self, event: AstrMessageEvent):
        from bridge.commands import toggle_group

        yield event.plain_result(toggle_group(self, self._gid(event), True))

    async def cmd_disable(self, event: AstrMessageEvent):
        from bridge.commands import toggle_group

        yield event.plain_result(toggle_group(self, self._gid(event), False))

    async def cmd_doctor(self, event: AstrMessageEvent):
        """自检: 全动态扫描, 结构化可观测, 错误显式返回 (复用 bridge.doctor)."""
        try:
            from bridge.doctor import render_text, run_checks, save_snapshot, summarize
            results = await run_checks()
            summary = summarize(results)
            report = render_text(results, summary)
            # 错误显式返回: 有失败项时附修复提示 + 快照落盘
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

    async def cmd_install_chromium(self, event: AstrMessageEvent):
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            yield event.plain_result("Chromium 已可用, 无需重复安装"); return
        except Exception: pass
        yield event.plain_result("开始安装 Chromium (耗时较长, 请等待)...")
        pb = str(Path(get_config().data_dir) / "playwright_browsers")
        installed = False
        for url, name in [("https://npmmirror.com/mirrors/playwright","npmmirror"),
                          ("https://playwright.azureedge.net","Azure")]:
            env = os.environ.copy(); env["PLAYWRIGHT_BROWSERS_PATH"] = pb
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
            try:
                yield event.plain_result(f"尝试 {name} ({url}) ...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium", env=env)
                await asyncio.wait_for(proc.wait(), timeout=600)
                if proc.returncode != 0:
                    yield event.plain_result(f"{name} 安装失败 (rc={proc.returncode}), 切换镜像...")
                    continue
                installed = True
                break
            except asyncio.TimeoutError:
                yield event.plain_result(f"{name} 超时, 切换镜像...")
            except Exception as e:
                yield event.plain_result(f"{name} 失败: {e}\n切换镜像...")
        if not installed:
            yield event.plain_result("✗ 浏览器下载失败, 请检查网络或手动执行: python -m playwright install chromium")
            return
        # 浏览器就绪 → 检查/补齐系统库 (install-deps 优先, apt-get 回退)
        missing = _detect_missing_libs()
        if missing:
            yield event.plain_result(f"检测到缺失系统库, 尝试自动安装:\n{missing}")
            if not (hasattr(os, "geteuid") and os.geteuid() == 0):
                yield event.plain_result(
                    "✗ 非 root 用户无法安装系统库, 请在容器/服务器以 root 运行:\n"
                    "  apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0\n"
                    "  或: python -m playwright install-deps chromium")
                return
            # ① playwright install-deps (全量依赖, 适配发行版包管理器)
            try:
                _deps_proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install-deps", "chromium",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _deps_out, _deps_err = await asyncio.wait_for(
                    _deps_proc.communicate(), timeout=600)
                if _deps_proc.returncode == 0:
                    astrbot_logger.info("[ParserLite] playwright install-deps 成功")
                else:
                    yield event.plain_result(
                        f"playwright install-deps 失败 (rc={_deps_proc.returncode}), "
                        f"回退 apt-get:\n{_deps_err.decode(errors='replace').strip()[-200:]}")
                    # ② 回退: 手写 apt-get 补齐核心库
                    try:
                        _apt1 = await asyncio.create_subprocess_exec(
                            "apt-get", "update",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await asyncio.wait_for(_apt1.communicate(), timeout=300)
                        _apt2 = await asyncio.create_subprocess_exec(
                            "apt-get", "install", "-y", "--no-install-recommends",
                            "libnspr4", "libnss3", "libgbm1", "libasound2", "libxkbcommon0",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        _o2, _e2 = await asyncio.wait_for(_apt2.communicate(), timeout=600)
                        if _apt2.returncode != 0:
                            yield event.plain_result(
                                f"✗ apt-get 安装失败: rc={_apt2.returncode} "
                                f"{_e2.decode(errors='replace').strip()[-300:]}")
                            yield event.plain_result(
                                "请手动执行: apt-get update && apt-get install -y "
                                "libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0")
                            return
                    except Exception as e:
                        yield event.plain_result(f"✗ apt-get 异常: {e}\n请手动安装系统库后重试")
                        return
            except asyncio.TimeoutError:
                yield event.plain_result("✗ playwright install-deps 超时, 请手动安装系统库后重试")
                return
            except Exception as e:
                yield event.plain_result(f"✗ 系统库安装异常: {e}\n请手动安装后重试")
                return
        # 最终验证
        try:
            from nonebot_plugin_parser_lite.utils.browser import BrowserManager
            await BrowserManager.ensure_started()
            yield event.plain_result("✓ Chromium 安装完成且可启动!")
        except Exception as e:
            yield event.plain_result(
                f"✗ Chromium 仍无法启动: {e}\n缺失库: {_detect_missing_libs() or '(none)'}\n"
                "请运行: python -m playwright install-deps chromium")

    async def cmd_bm(self, event: AstrMessageEvent):
        """下载 B站音频: 从当前消息 / 懒下载会话 / 回复消息 三路提取 BV 号"""
        text = event.get_message_str()
        bvid = None

        # 1) 当前消息直接匹配
        m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", text)
        if m: bvid = m.group(0)

        # 2) 懒下载会话中提取 (先回复了 bilibili 链接, 再发 cmd_bm)
        if not bvid:
            session = LazyManager.get(self._key(event))
            if session and session.url:
                m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", session.url)
                if m: bvid = m.group(0)

        # 3) 从被回复的消息中提取 BV (上游 BvReplyMergeExtension 等价实现)
        if not bvid:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                raw_segs = getattr(msg_obj, "message", None) or []
                for seg in (raw_segs if isinstance(raw_segs, list) else []):
                    if isinstance(seg, dict) and seg.get("type") == "reply":
                        reply_data = seg.get("data", {})
                        reply_text = reply_data.get("text", "") or reply_data.get("message", "") or ""
                        m = re.search(r"[Bb][Vv][A-Za-z0-9]{10}", str(reply_text))
                        if m:
                            bvid = m.group(0)
                            break

        if not bvid:
            yield event.plain_result("未找到BV号 (当前消息/懒下载会话/回复消息均无)"); return

        from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser
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

    async def cmd_blogin(self, event: AstrMessageEvent):
        from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser
        bili = BilibiliParser()
        try:
            qr_bytes = await bili.login_with_qrcode()
            yield event.plain_result("B站登录二维码已生成, 请用手机B站扫描以下二维码:")
            yield event.chain_result([Comp.Image.fromBytes(qr_bytes)])
        except Exception as e:
            yield event.plain_result(f"Error: {e}")

    async def parse_url(self, event: AstrMessageEvent, url: str) -> str:
        if self._blacklisted(event): return "黑名单用户"
        cfg = get_config()
        disabled = cfg.disabled_platforms
        for d in disabled:
            if isinstance(d, str): d_name = d.lower()
            else: d_name = d.name.lower() if hasattr(d, "name") else str(d).lower()
            if d_name:
                for cls in BaseParser.get_all_subclass():
                    p = getattr(cls, "platform", None)
                    if p and p.name.lower() == d_name:
                        return f"{p.display_name} 已禁用"
        result = await self._parse_and_format(url)
        return result or "无法解析该链接"

# ── 装饰器注册 ────────────────────────────────────────────────────────────────
filter.command("parse")(ParserLitePlugin.cmd_parse)
filter.command("parse_dl")(ParserLitePlugin.cmd_parse_dl)
filter.command("parse_clean")(ParserLitePlugin.cmd_clean)
filter.command("parse_status")(ParserLitePlugin.cmd_status)
filter.command("parse_enable")(ParserLitePlugin.cmd_enable)
filter.command("parse_disable")(ParserLitePlugin.cmd_disable)
filter.command("parse_doctor")(ParserLitePlugin.cmd_doctor)
filter.command("parser_doctor")(ParserLitePlugin.cmd_doctor)  # 别名 (用户习惯 /parser_doctor)
filter.command("parse_install_chromium")(ParserLitePlugin.cmd_install_chromium)
filter.command("cmd_bm")(ParserLitePlugin.cmd_bm)
filter.command("cmd_blogin")(ParserLitePlugin.cmd_blogin)
filter.regex(r"^(xz|下载)$")(ParserLitePlugin._on_download_trigger)
filter.regex(r"https?://")(ParserLitePlugin.on_url_auto)
filter.llm_tool(name="parse_url")(ParserLitePlugin.parse_url)
