"""频率限制 — 同链接/同用户 时间窗+次数, JSON 持久化.

配置驱动: 通过 configure(plite_rate_limit=...) 或 _source["rate_limit"] 传入
    {"enabled": true, "max_per_window": 5, "window_seconds": 300,
     "max_per_user_window": 10}
并发安全: JsonStateStore (锁 + 写节流 + 原子落盘)
"""

from __future__ import annotations

from pathlib import Path
import time

from bridge.state_store import JsonStateStore

_MAX_PER_KEY = 50  # 每个 key 内存上限 (含 prune 语义, 超出即截断)


class RateLimiter:
    def __init__(self, state_path: str | Path | None = None):
        # 即时落盘 (原子写): 限频计数跨重启即时性优先
        self._store = JsonStateStore(state_path, flush_every=1, flush_interval=0.5)
        self._hits: dict[str, list[float]] = self._store.data  # 共享 dict 引用

    def save(self) -> None:
        """显式落盘 (兼容旧调用)."""
        self._store.flush()

    def _hit(self, key: str, window: float) -> int:
        now = time.time()

        def _record(d: dict):
            _pruned = [t for t in d.get(key, []) if now - t < window]
            _pruned.append(now)
            # 限制内存: 每 key 最多 _MAX_PER_KEY 条
            if len(_pruned) >= _MAX_PER_KEY:
                _pruned = _pruned[-_MAX_PER_KEY:]
            d[key] = _pruned

        self._store.update(_record)
        return len(self._hits.get(key, []))

    def allow(
        self, *, url: str, user_id: str = "", cfg: dict | None = None
    ) -> tuple[bool, str]:
        """检查是否允许解析.

        :return: (allowed, reason) — allowed=False 时 reason 为拒绝原因
        """
        cfg = cfg or {}
        if not cfg.get("enabled", False):
            return True, ""
        window = float(cfg.get("window_seconds", 300))
        max_per_window = int(cfg.get("max_per_window", 5))
        max_per_user = int(cfg.get("max_per_user_window", 20))

        url_count = self._hit(f"url:{url}", window)
        if url_count > max_per_window:
            return False, f"同一链接解析过于频繁, 请 {int(window)} 秒后再试"

        if user_id:
            user_count = self._hit(f"user:{user_id}", window)
            if user_count > max_per_user:
                return False, "解析频率超限, 请稍后再试"
        return True, ""


def clean_url(url: str) -> str:
    """清洗追踪参数, 提高去重/限频命中率."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return url
        tracking = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "share_token",
            "share_source",
            "share_medium",
            "share_plat",
            "share_session_id",
            "spm_id_from",
            "from_source",
            "from",
            "source",
            "timestamp",
            "ts",
        }
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in tracking
        ]
        cleaned = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )
        return cleaned or url
    except Exception:
        return url


def make_limiter(base_dir: str | Path) -> RateLimiter:
    return RateLimiter(Path(base_dir) / "rate_limit.json")


def load_rate_cfg(source: dict | None) -> dict:
    """从 bridge 配置源提取限频配置 (复用 cfg.module_cfg 统一解析)."""
    from bridge.cfg import global_source, module_cfg

    src = source if source is not None else global_source()
    raw = module_cfg(src, "rate_limit", {})
    return raw if isinstance(raw, dict) else {}
