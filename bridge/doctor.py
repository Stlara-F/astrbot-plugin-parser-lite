"""ParserLite 自检模块 (parser_doctor 核心) — 结构化、可观测、错误显式返回.

设计:
- check_*() 每个返回 CheckResult(ok, label, detail, duration, error)
- run_checks() 聚合全部, 返回可 JSON 序列化结果 (供测试/命令渲染)
- 0 硬编码: 检查项声明式注册, 网络探测地址可配置
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time


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


async def _run_check(name: str, fn: Callable[[], Awaitable[tuple[bool, str, bool]]]) -> CheckResult:
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
    from bridge.core import get_config

    cfg = get_config()
    if cfg is None:
        return False, "config 未初始化", False
    return True, f"{len(cfg.model_fields)} fields, cache={cfg.cache_dir}", False


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
    """Chromium 可用性 (缺失警告, 有超时防 hang)."""
    from nonebot_plugin_parser_lite.utils.browser import BrowserManager

    try:
        await asyncio.wait_for(BrowserManager.ensure_started(), timeout=timeout)
        return True, "ready", False
    except asyncio.TimeoutError:
        return False, f"启动超时 ({timeout}s)", True
    except Exception as e:
        return False, str(e)[:100], True


async def check_network(probe_url: str = "https://www.bilibili.com") -> tuple[bool, str, bool]:
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

    parser_set = {_norm(getattr(cls, "platform", None).name)
                  for cls in BaseParser.get_all_subclass()
                  if getattr(cls, "platform", None)}
    missing = enum_set - parser_set
    # 缺失平台是信息而非致命错误 (部分平台可能由聚合 parser 覆盖)
    ok = not missing
    detail = f"{len(parser_set)}/{len(enum_set)} platforms"
    if missing:
        detail += f", missing: {', '.join(sorted(missing))}"
    return ok, detail, bool(missing)  # 缺失 → warn


async def check_route_table() -> tuple[bool, str, bool]:
    """特征路由表."""
    from bridge.core import FEATURE_TABLE, _build_feature_table

    _build_feature_table()
    ok = len(FEATURE_TABLE) > 0
    return ok, f"{len(FEATURE_TABLE)} keywords", False


async def check_render() -> tuple[bool, str, bool]:
    """渲染管线 (render import + safe_src patch)."""
    try:
        from bridge.render_patch import apply_render_patch
        applied = apply_render_patch()
        from nonebot_plugin_parser_lite.render import RENDERER
        return True, f"templates={RENDERER.templates_dir}, patch={applied}", False
    except Exception as e:
        return False, str(e), False


async def check_schema() -> tuple[bool, str, bool]:
    """注入 schema 完整 (commit gate 复用)."""
    try:
        from bridge.core import BridgeConfig

        src = BridgeConfig._source or {}
        if not src:
            return False, "未初始化 (首次运行注入)", True  # 警告非致命
        required = ["plite_http_proxy", "send_strategy", "plite_dedup_ttl"]
        missing = [k for k in required if k not in src]
        ok = not missing
        return ok, f"{len(src)} config keys" + (f", missing: {missing}" if missing else ""), not ok
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
    lines.append(f"── 摘要: {summary['ok']}/{summary['total']} OK, "
                 f"{summary['warn']} warn, {summary['failed']} fail ──")
    if summary["failed_items"]:
        lines.append(f"失败项: {', '.join(summary['failed_items'])}")
    return "\n".join(lines)
