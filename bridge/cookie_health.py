"""Cookie 健康检查 (F4) — 定期验证 B站/知乎 cookie 有效性, 失效时提示.

不进行签名逆向 (z_c0 续期需外部库), 只做:
1. 定期调用公开 API 验证 cookie (nav 接口)
2. 失效 → 日志 + 可选回调通知 (提示重新扫码)

0 硬编码: cookie 从配置动态读取, 检查间隔动态配置.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path

from nonebot_plugin_parser_lite.utils.bilibili.client import CLIENT

logger = logging.getLogger("parser-lite.bridge.cookie")

_NotifyFn = Callable[[str], Awaitable[None]]


async def check_bili_cookie(ck: str) -> tuple[bool, str]:
    """验证 B站 cookie: 调 nav 接口, 返回 (有效, 昵称/错误)."""
    if not ck or not ck.strip():
        return False, "未配置"
    try:
        resp = await CLIENT.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": ck},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            uname = ((data.get("data") or {}).get("uname")) or ""
            return True, uname
        return False, str(data.get("message", "cookie 无效"))
    except Exception as e:
        return False, str(e)


async def check_zhihu_cookie(ck: str) -> tuple[bool, str]:
    """验证知乎 cookie (z_c0): 调个人信息接口."""
    if not ck or not ck.strip():
        return False, "未配置"
    try:
        resp = await CLIENT.get(
            "https://www.zhihu.com/api/v4/me",
            headers={"Cookie": ck, "User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "有效"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


class CookieHealth:
    def __init__(self, state_path: str | Path | None = None):
        from bridge.state_store import JsonStateStore

        self._store = JsonStateStore(state_path)
        self._last_status: dict[str, dict] = self._store.data
        self._task: asyncio.Task | None = None
        self._notify: _NotifyFn | None = None

    def save(self) -> None:
        """显式落盘 (兼容旧调用)."""
        self._store.flush()

    async def check_once(self, cookies: dict[str, str]) -> None:
        """检查所有已配置平台 cookie, 状态变化时通知."""
        for platform, ck in cookies.items():
            if not ck or not ck.strip():
                continue
            if platform == "bilibili":
                ok, info = await check_bili_cookie(ck)
            elif platform == "zhihu":
                ok, info = await check_zhihu_cookie(ck)
            else:
                continue
            prev = self._last_status.get(platform, {}).get("ok")
            changed = prev is None or prev != ok

            def _set(d: dict):
                d[platform] = {"ok": ok, "info": info, "ts": __import__("time").time()}

            self._store.update(_set)
            if not ok and changed and self._notify:
                try:
                    await self._notify(f"[ParserLite] {platform} cookie 失效: {info}, 请重新扫码登录")
                except Exception as e:
                    logger.warning(f"[ParserLite] cookie 通知失败: {e}")
            elif ok and changed:
                logger.info(f"[ParserLite] {platform} cookie 有效: {info}")

    async def run(self, interval_sec: float, cookies: dict[str, str], notify: _NotifyFn) -> None:
        self._notify = notify
        while True:
            try:
                await self.check_once(cookies)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ParserLite] cookie 检查异常: {e}")
            await asyncio.sleep(interval_sec)

    def start(self, interval_sec: float, cookies: dict[str, str], notify: _NotifyFn) -> None:
        self._task = asyncio.create_task(self.run(interval_sec, cookies, notify))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def make_cookie_health(base_dir: str | Path) -> CookieHealth:
    return CookieHealth(Path(base_dir) / "cookie_health.json")


def load_cfg(source: dict | None = None) -> dict:
    """提取 cookie_health 配置段 (功能自包含, 可注入配置源)."""
    from bridge.cfg import global_source, module_cfg

    src = source if source is not None else global_source()
    cfg = module_cfg(src, "cookie_health", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "interval_sec": int(cfg.get("interval_sec", 3600) or 3600),
    }
