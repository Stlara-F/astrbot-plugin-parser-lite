"""AstrBot 命令业务 (扩展层, 与上游解耦; r11: main.py 全委托).

设计: 命令函数 (plugin, event) 注入模式, 返回 AsyncIterator 逐条 yield
消息段; main.py 仅 `async for msg in commands.x(self, event): yield msg`
薄委托转发, 事件/命令注册 (filter.command 类方法绑定) 保持.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Any


def status_text(plugin: Any) -> str:
    """状态报告 (纯数据, 插件实例注入; r8: 自研缓存/懒下载会话已删)."""
    from bridge.adapter import up_base_parser
    from bridge.config import get_config
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
    from bridge.config import _save_disabled_groups

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
    from bridge.config import get_config

    try:
        return event.get_sender_id() in get_config().blacklist_users
    except Exception:
        return False


def _extract_urls(event) -> list[str]:
    """消息 URL 提取 (主链路 + 引用消息逃生通道)."""
    import astrbot.api.message_components as _Comp

    from bridge.adapter import extract_reply_urls, extract_urls

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

    from bridge.commands import ensure_chromium
    from bridge.config import get_config

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
        from bridge.commands import render_text, run_checks, save_snapshot, summarize

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


# ── 自检 (doctor 合并) ───────────────────────────────────────
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    error: str = ""
    duration: float = 0.0
    warn: bool = False  # ok=False 但非致命 (如 Chromium 未装)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "error": self.error,
            "duration": round(self.duration, 3),
            "warn": self.warn,
        }


async def _run_check(
    name: str, fn: Callable[[], Awaitable[tuple[bool, str, bool]]]
) -> CheckResult:
    t0 = time.time()
    try:
        ok, detail, warn = await fn()
        return CheckResult(name, ok, detail, duration=time.time() - t0, warn=warn)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return CheckResult(name, False, "", str(e), time.time() - t0)


async def check_config() -> tuple[bool, str, bool]:
    """配置可用性."""
    from bridge.config import get_config

    cfg = get_config()
    if cfg is None:
        return False, "config 未初始化", False
    return True, f"{len(type(cfg).model_fields)} fields, cache={cfg.cache_dir}", False


async def check_ffmpeg() -> tuple[bool, str, bool]:
    """ffmpeg 可用性 (缺失警告不致命)."""
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

    avail = await FFmpeg.is_available()
    if avail:
        return True, "可用", False
    return False, "缺失 (apt install ffmpeg)", True


async def check_downloader() -> tuple[bool, str, bool]:
    """下载器可用性."""
    from nonebot_plugin_parser_lite.download import DOWNLOADER

    try:
        _ = DOWNLOADER.client
        return True, "ready", False
    except Exception as e:
        return False, str(e), False


async def check_chromium(timeout: float = 10.0) -> tuple[bool, str, bool]:
    """Chromium 可用性 (缺失警告, 有超时防 hang, 缺库时给 apt 修复命令)."""
    from nonebot_plugin_parser_lite.utils.browser import BrowserManager

    try:
        await asyncio.wait_for(BrowserManager.ensure_started(), timeout=timeout)
        return True, "ready", False
    except asyncio.TimeoutError:
        return False, f"启动超时 ({timeout}s)", True
    except Exception as e:
        detail = str(e)[:120]
        missing = _detect_missing_libs_hint()
        if missing:
            detail += f"; 缺失系统库: {missing} → apt-get update && apt-get install -y libnspr4 libnss3 libgbm1 libasound2 libxkbcommon0"
        return False, detail, True


def _detect_missing_libs_hint() -> str:
    """复用 bridge.config._detect_missing_libs 检测缺失库 (无 astrbot 依赖)."""
    try:
        from bridge.config import _detect_missing_libs

        return _detect_missing_libs().strip() or ""
    except Exception:
        return ""


async def check_network(
    probe_url: str = "https://www.bilibili.com",
) -> tuple[bool, str, bool]:
    """网络可达性 (探测地址可配置, 默认国内可达站点)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
            r = await c.get(probe_url)
        return r.status_code < 500, f"HTTP {r.status_code}", False
    except Exception as e:
        return False, str(e)[:100], False


async def check_parsers() -> tuple[bool, str, bool]:
    """解析器注册完整性."""
    from nonebot_plugin_parser_lite.parsers import load_all as _load_all
    from nonebot_plugin_parser_lite.parsers.base import BaseParser

    _load_all()  # 惰性发现 → 显式注册
    parsers = list(BaseParser.get_all_subclass())
    broken = []
    for cls in parsers:
        try:
            cls()
            if not getattr(cls, "platform", None):
                broken.append(f"{cls.__name__}: missing platform")
        except Exception as e:
            broken.append(f"{cls.__name__}: {e}")
    ok = not broken
    detail = f"{len(parsers)} total, {len(parsers) - len(broken)} ok"
    if broken:
        detail += f", broken: {', '.join(broken[:5])}"
    return ok, detail, False


async def check_coverage() -> tuple[bool, str, bool]:
    """平台覆盖 (PlatformEnum vs 已注册 parser).

    兼容拼写变体: enum 'fiveeplay' ↔ parser '5eplay'.
    """
    from nonebot_plugin_parser_lite.constants import PlatformEnum
    from nonebot_plugin_parser_lite.parsers import load_all as _load_all
    from nonebot_plugin_parser_lite.parsers.base import BaseParser

    _load_all()  # 惰性发现 → 显式注册
    enum_set = {p.name.lower() for p in PlatformEnum}

    def _norm(name: str) -> str:
        return name.lower().replace("5eplay", "fiveeplay")

    parser_set = {
        _norm(getattr(cls, "platform", None).name)
        for cls in BaseParser.get_all_subclass()
        if getattr(cls, "platform", None)
    }
    missing = enum_set - parser_set
    # 缺失平台是信息而非致命错误 (部分平台可能由聚合 parser 覆盖)
    ok = not missing
    detail = f"{len(parser_set)}/{len(enum_set)} platforms"
    if missing:
        detail += f", missing: {', '.join(sorted(missing))}"
    return ok, detail, bool(missing)  # 缺失 → warn


async def check_route_table() -> tuple[bool, str, bool]:
    """匹配能力 (r8: 委托上游 pipeline.Parser.match 验证)."""
    try:
        from nonebot_plugin_parser_lite.pipeline import Parser

        m = Parser().match("https://www.bilibili.com/video/BV1GJ411x7h7")
        ok = m is not None
        return ok, f"match={'ok' if ok else 'fail'}", not ok
    except Exception as e:
        return False, f"match error: {e}", True


async def check_render() -> tuple[bool, str, bool]:
    """渲染管线 (render import + safe_src patch)."""
    try:
        from bridge.render import apply_render_patch

        applied = apply_render_patch()
        from nonebot_plugin_parser_lite.render import RENDERER

        return True, f"templates={RENDERER.templates_dir}, patch={applied}", False
    except Exception as e:
        return False, str(e), False


async def check_schema() -> tuple[bool, str, bool]:
    """注入 schema 完整 (commit gate 复用)."""
    try:
        from bridge.config import global_source

        src = global_source()
        if not src:
            return False, "未初始化 (首次运行注入)", True  # 警告非致命
        required = ["send_strategy", "plite_direct_link"]  # r9: 现存键
        missing = [k for k in required if k not in src]
        ok = not missing
        return (
            ok,
            f"{len(src)} config keys" + (f", missing: {missing}" if missing else ""),
            not ok,
        )
    except Exception as e:
        return False, str(e), False


CHECK_REGISTRY: list[tuple[str, Callable[[], Awaitable[tuple[bool, str, bool]]]]] = [
    ("config", check_config),
    ("ffmpeg", check_ffmpeg),
    ("downloader", check_downloader),
    ("chromium", check_chromium),
    ("network", check_network),
    ("parsers", check_parsers),
    ("coverage", check_coverage),
    ("route_table", check_route_table),
    ("render", check_render),
    ("schema", check_schema),
]


async def run_checks() -> list[CheckResult]:
    """运行全部检查, 返回结构化结果 (可观测)."""
    results: list[CheckResult] = []
    for name, fn in CHECK_REGISTRY:
        results.append(await _run_check(name, fn))
    return results


def summarize(results: list[CheckResult]) -> dict:
    """聚合摘要: 计数 + 失败项 (错误显式返回)."""
    total = len(results)
    ok = sum(1 for r in results if r.ok)
    warn = sum(1 for r in results if r.warn)
    failed = total - ok - warn
    return {
        "total": total,
        "ok": ok,
        "warn": warn,
        "failed": failed,
        "failed_items": [r.name for r in results if not r.ok and not r.warn],
        "all_ok": failed == 0,
    }


def render_text(results: list[CheckResult], summary: dict) -> str:
    """渲染人类可读报告."""
    lines = ["=== ParserLite Doctor ===", ""]
    for r in results:
        icon = "OK" if r.ok else ("WARN" if r.warn else "FAIL")
        lines.append(f"[{icon}] {r.name}: {r.detail} ({r.duration:.2f}s)")
        if r.error:
            lines.append(f"       error: {r.error}")
    lines.append("")
    lines.append(
        f"── 摘要: {summary['ok']}/{summary['total']} OK, "
        f"{summary['warn']} warn, {summary['failed']} fail ──"
    )
    if summary["failed_items"]:
        lines.append(f"失败项: {', '.join(summary['failed_items'])}")
    return "\n".join(lines)


def to_json(results: list[CheckResult], summary: dict | None = None) -> str:
    """JSON 序列化 (机器可解析, CI/日志可观测)."""
    import json

    payload = {
        "checks": [r.to_dict() for r in results],
        "summary": summary if summary is not None else summarize(results),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def save_snapshot(
    results: list[CheckResult], summary: dict, target: str | None = None
) -> str | None:
    """错误快照落盘 (失败详情显式持久化, 便于事后排查).

    :param target: 输出路径 (默认: 插件 data_dir/doctor_snapshot.json)
    :return: 快照路径; 全部 OK 时返回 None (无失败不落盘)
    """
    if summary["failed"] == 0 and summary["warn"] == 0:
        return None
    try:
        if target is None:
            from bridge.config import get_config

            cfg = get_config()
            base = Path(cfg.data_dir) if cfg is not None else Path(".")
            target = str(base / "doctor_snapshot.json")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(to_json(results, summary), encoding="utf-8")
        return target
    except Exception:
        return None


# ── Chromium 安装 (browser 合并) ─────────────────────────────────
# 镜像下载源 (npmmirror 优先, Azure 回退)
MIRRORS: list[tuple[str, str]] = [
    ("https://npmmirror.com/mirrors/playwright", "npmmirror"),
    ("https://playwright.azureedge.net", "Azure"),
]

# Chromium 运行所需系统库 (apt-get 补齐清单, 与 install-deps 交集)
SYSTEM_LIBS: list[str] = [
    "libnspr4",
    "libnss3",
    "libgbm1",
    "libasound2",
    "libxkbcommon0",
]

INSTALL_TIMEOUT = 600  # 浏览器下载/安装超时(秒)
DEPS_TIMEOUT = 600  # playwright install-deps 超时(秒)
APT_UPDATE_TIMEOUT = 300  # apt-get update 超时(秒)
APT_INSTALL_TIMEOUT = 600  # apt-get install 超时(秒)


def _is_root() -> bool:
    """root 判定 (Windows 无 geteuid → False)."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _log(log, msg: str) -> None:
    """按消息前缀分级记录日志 (后台侧)."""
    if log is None:
        return
    if msg.startswith("✗"):
        log.error(f"[ParserLite] {msg}")
    elif any(k in msg for k in ("失败", "超时", "异常", "无法启动")):
        log.warning(f"[ParserLite] {msg}")
    else:
        log.info(f"[ParserLite] {msg}")


async def _verify() -> bool:
    """BrowserManager 验证 Chromium 可启动 (复用上游单例)."""
    try:
        from nonebot_plugin_parser_lite.utils.browser import BrowserManager

        await BrowserManager.ensure_started()
        return True
    except Exception:
        return False


async def _download_chromium(browsers_path: str, messages: list[str]) -> bool:
    """镜像循环下载 (环境注入 PLAYWRIGHT_BROWSERS_PATH/PLAYWRIGHT_DOWNLOAD_HOST)."""
    for url, name in MIRRORS:
        env = os.environ.copy()
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
        if browsers_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        messages.append(f"尝试 {name} ({url}) ...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=INSTALL_TIMEOUT
            )
            if proc.returncode == 0:
                messages.append(f"Chromium 安装完成 ({name})")
                return True
            err = stderr.decode(errors="replace").strip()[-300:]
            messages.append(f"Chromium 安装失败 ({name}): rc={proc.returncode} {err}")
        except asyncio.TimeoutError:
            messages.append(f"Chromium 安装超时 ({name}), 切换镜像...")
        except Exception as e:
            messages.append(f"Chromium 安装异常 ({name}): {e}")
    return False


async def _install_system_libs(messages: list[str]) -> bool:
    """系统库补齐: playwright install-deps 优先, apt-get 回退.

    P1-6: apt-get 输出量大, PIPE 缓冲会死锁 → 重定向到 DEVNULL.
    """
    from bridge.config import _detect_missing_libs

    missing = _detect_missing_libs()
    if not missing:
        return True
    messages.append(f"检测到缺失系统库, 尝试自动安装:\n{missing}")
    if not _is_root():
        messages.append(
            "✗ 非 root 用户无法安装系统库, 请在容器/服务器以 root 运行:\n"
            "  apt-get update && apt-get install -y "
            + " ".join(SYSTEM_LIBS)
            + "\n  或: python -m playwright install-deps chromium"
        )
        return False
    # ① playwright install-deps (全量依赖, 适配发行版包管理器)
    try:
        _deps_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install-deps",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _deps_out, _deps_err = await asyncio.wait_for(
            _deps_proc.communicate(), timeout=DEPS_TIMEOUT
        )
        if _deps_proc.returncode == 0:
            return True
        messages.append(
            f"playwright install-deps 失败 (rc={_deps_proc.returncode}), "
            f"回退 apt-get:\n{_deps_err.decode(errors='replace').strip()[-200:]}"
        )
    except asyncio.TimeoutError:
        messages.append("✗ playwright install-deps 超时, 回退 apt-get...")
    except Exception as e:
        messages.append(f"✗ 系统库安装异常: {e}, 回退 apt-get...")
    # ② 回退: 手写 apt-get 补齐核心库 (DEVNULL 防死锁)
    try:
        _apt1 = await asyncio.create_subprocess_exec(
            "apt-get",
            "update",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(_apt1.communicate(), timeout=APT_UPDATE_TIMEOUT)
        _apt2 = await asyncio.create_subprocess_exec(
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *SYSTEM_LIBS,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(_apt2.communicate(), timeout=APT_INSTALL_TIMEOUT)
        if _apt2.returncode != 0:
            messages.append(
                f"✗ apt-get 安装失败: rc={_apt2.returncode}\n"
                "请手动执行: apt-get update && apt-get install -y "
                + " ".join(SYSTEM_LIBS)
            )
            return False
        return True
    except asyncio.TimeoutError:
        messages.append("✗ apt-get 安装系统库超时, 请手动安装后重试")
        return False
    except Exception as e:
        messages.append(f"✗ apt-get 异常: {e}\n请手动安装系统库后重试")
        return False


async def ensure_chromium(
    browsers_path: str = "", log=None, started_msg: str = "Chromium 已就绪"
) -> tuple[bool, list[str]]:
    """统一编排: 验证 → 镜像下载 → 系统库补齐 → 最终验证.

    :param browsers_path: PLAYWRIGHT_BROWSERS_PATH (空则沿用环境已设置值)
    :param log: 日志对象 (后台侧按消息分级记录; 命令侧传 None, 逐条回显)
    :param started_msg: 验证通过时的成功提示 (后台/命令语境差异)
    :return: (ok, messages) — messages 为逐步回显文本列表
    """
    from bridge.config import _detect_missing_libs

    messages: list[str] = []
    try:
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
    except Exception:
        pass
    messages.append("Chromium 未安装, 开始异步安装...")
    installed = await _download_chromium(browsers_path, messages)
    if installed:
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
        messages.append("✗ Chromium 已下载但无法启动, 尝试补齐系统库...")
    # 下载失败或缺库 → 系统库补齐 (install-deps 优先 / apt-get 回退)
    if await _install_system_libs(messages):
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
    # 最终失败: 显式列出缺失库 + 修复指引
    _missing_now = _detect_missing_libs()
    messages.append(
        "✗✗ Chromium 环境自动组装失败, 卡片渲染将回退为文本 ✗✗\n"
        f"缺失系统库:\n{_missing_now or '(未检测到缺失库, 请检查 playwright 安装)'}\n"
        "修复方式(需容器 root):\n"
        "  1) apt-get update && apt-get install -y " + " ".join(SYSTEM_LIBS) + "\n"
        "  2) 或运行: python -m playwright install-deps chromium\n"
        "  3) 或发送指令 /parse_install_chromium 重试浏览器下载"
    )
    for m in messages:
        _log(log, m)
    return False, messages
