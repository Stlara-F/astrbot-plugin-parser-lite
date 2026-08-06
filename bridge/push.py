"""B站 UP 动态/直播订阅主动推送 (F1) — 独立模块.

- 轮询: asyncio 常驻任务, 间隔动态配置
- 去重: 时间戳滑动窗口 (动态 id 集合), JSON 持久化
- 订阅: {uid: [group_ids]} 配置驱动
- 发送: 回调注入 (由 main.py 提供 context.send_message 包装)
- 0 硬编码: 所有参数从配置读取, 缺失用安全默认值
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path

from nonebot_plugin_parser_lite.utils.bilibili.client import CLIENT

logger = logging.getLogger("parser-lite.bridge.push")

_SendFn = Callable[[str, list[str]], Awaitable[None]]
"""send(platform_msg, [group_ids]) — 由宿主注入"""


class UpPusher:
    def __init__(self, state_path: str | Path | None = None):
        from bridge.state_store import JsonStateStore

        self._store = JsonStateStore(state_path)
        _d = self._store.data
        self._seen_dynamics: dict[str, list[str]] = _d.setdefault("seen_dynamics", {})
        self._live_status: dict[str, bool] = _d.setdefault("live_status", {})
        self._subs: dict[str, list[str]] = _d.setdefault("subs", {})
        self._task: asyncio.Task | None = None
        self._send: _SendFn | None = None

    # ── 持久化 (JsonStateStore: 锁 + 写节流 + 原子落盘) ──
    def _persist(self) -> None:
        def _set(d: dict):
            d["seen_dynamics"] = self._seen_dynamics
            d["live_status"] = self._live_status
            d["subs"] = self._subs

        self._store.update(_set)

    def save(self) -> None:
        """显式落盘 (兼容旧调用)."""
        self._store.flush()

    # ── 订阅管理 ──
    def set_subscriptions(self, subs: dict[str, list[str]]) -> None:
        """更新订阅: {uid: [group_ids]} (全量替换, 由配置驱动)."""
        self._subs = {str(k): [str(g) for g in v] for k, v in subs.items()}
        self._persist()

    def get_subscriptions(self) -> dict[str, list[str]]:
        return dict(self._subs)

    # ── 轮询核心 ──
    async def _fetch_dynamics(self, uid: str) -> list[dict]:
        """获取用户最新动态 (公开 API, 无需 cookie)."""
        try:
            resp = await CLIENT.get(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                params={"host_mid": uid},
                timeout=15,
            )
            data = resp.json()
            items = (data.get("data") or {}).get("items") or []
            return items
        except Exception as e:
            logger.warning(f"[ParserLite] 动态获取失败 uid={uid}: {e}")
            return []

    @staticmethod
    def _dynamic_info(item: dict) -> tuple[str, str] | None:
        """从动态 item 提取 (dynamic_id, 文本预览). 0 硬编码: 字段用 get 兜底."""
        try:
            modules = item.get("modules", {})
            desc = (modules.get("module_dynamic") or {}).get("desc", {})
            did = str(desc.get("dynamic_id", ""))
            text = (modules.get("module_desc") or {}).get("text", "") or ""
            if not did:
                return None
            return did, text[:80]
        except Exception:
            return None

    async def _fetch_live(self, uids: list[str]) -> dict[str, bool]:
        """批量查询直播状态."""
        if not uids:
            return {}
        try:
            resp = await CLIENT.get(
                "https://api.live.bilibili.com/xlive/web-interface/card?user_ids="
                + ",".join(uids),
                timeout=15,
            )
            data = resp.json()
            cards = data.get("data") or {}
            return {
                str(uid): bool((cards.get(uid) or {}).get("live_status") == 1)
                for uid in uids
            }
        except Exception as e:
            logger.warning(f"[ParserLite] 直播状态获取失败: {e}")
            return {}

    async def poll_once(self) -> None:
        """单轮轮询: 动态 + 直播, 有更新则回调发送."""
        if not self._send:
            return
        for uid, groups in self._subs.items():
            seen = self._seen_dynamics.get(uid, [])
            items = await self._fetch_dynamics(uid)
            new_items = []
            for item in items:
                info = self._dynamic_info(item)
                if not info:
                    continue
                did, text = info
                if did in seen:
                    continue
                new_items.append((did, text))
                seen.append(did)
            # 首次订阅只记录不推送 (避免历史刷屏)
            if seen and new_items:
                self._seen_dynamics[uid] = seen[-50:]
                self._persist()
                for did, text in new_items[-3:]:  # 最多推送最近 3 条
                    try:
                        await self._send(
                            f"[B站动态] UP{uid}\n{text}\nhttps://t.bilibili.com/{did}",
                            groups,
                        )
                    except Exception as e:
                        logger.warning(f"[ParserLite] 动态推送失败: {e}")
            elif not seen and items:
                self._seen_dynamics[uid] = [
                    did for did, _ in [self._dynamic_info(i) for i in items] if did
                ]
                self._persist()

        # 直播状态
        live = await self._fetch_live(list(self._subs.keys()))
        for uid, is_living in live.items():
            prev = self._live_status.get(uid, False)
            if is_living and not prev:
                groups = self._subs.get(uid, [])
                try:
                    await self._send(
                        f"[B站直播] UP{uid} 开播了!\nhttps://live.bilibili.com", groups
                    )
                except Exception as e:
                    logger.warning(f"[ParserLite] 直播推送失败: {e}")
            self._live_status[uid] = is_living
        self._persist()

    async def run(self, interval_sec: float, send_fn: _SendFn) -> None:
        """常驻轮询循环 (宿主在 initialize 启动, terminate 取消)."""
        self._send = send_fn
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ParserLite] 轮询异常: {e}")
            await asyncio.sleep(interval_sec)

    def start(self, interval_sec: float, send_fn: _SendFn) -> None:
        self._task = asyncio.create_task(self.run(interval_sec, send_fn))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def make_pusher(base_dir: str | Path) -> UpPusher:
    return UpPusher(Path(base_dir) / "push_state.json")


def load_cfg(source: dict | None = None) -> tuple[list[dict], int]:
    """提取 push 配置段: (订阅列表, 轮询间隔秒).

    订阅: template_list [{uid, groups, enabled}] 或旧 dict {uid: [groups]}.
    """
    from bridge.cfg import global_source, module_cfg

    src = source if source is not None else global_source()
    raw = module_cfg(src, "push", []) or []
    interval = int(module_cfg(src, "push_interval", 300) or 300)
    subs: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                subs.append(entry)
    elif isinstance(raw, dict):
        for uid, groups in raw.items():
            subs.append(
                {
                    "uid": str(uid),
                    "groups": ",".join(str(g) for g in groups),
                    "enabled": True,
                }
            )
    return subs, interval
