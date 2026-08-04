"""频率限制 — 同链接/同用户 时间窗+次数, JSON 持久化.

配置驱动: 通过 configure(plite_rate_limit=...) 或 _source["rate_limit"] 传入
    {"enabled": true, "max_per_window": 5, "window_seconds": 300,
     "max_per_user_window": 10}
"""

from __future__ import annotations

import json
from pathlib import Path
import time


class RateLimiter:
    def __init__(self, state_path: str | Path | None = None):
        self._state_path = Path(state_path) if state_path else None
        self._hits: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text("utf-8"))
            self._hits = data.get("hits", {})
        except Exception:
            self._hits = {}

    def save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"hits": self._hits}), encoding="utf-8"
            )
        except Exception:
            pass

    def _prune(self, key: str, window: float) -> None:
        now = time.time()
        self._hits[key] = [t for t in self._hits.get(key, []) if now - t < window]

    def _hit(self, key: str, window: float) -> int:
        now = time.time()
        self._prune(key, window)
        self._hits.setdefault(key, []).append(now)
        # 限制内存: 每个 key 最多 50 条
        if len(self._hits[key]) > 50:
            self._hits[key] = self._hits[key][-50:]
        self.save()
        return len(self._hits[key])

    def allow(self, *, url: str, user_id: str = "", cfg: dict | None = None) -> tuple[bool, str]:
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
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "share_token", "share_source", "share_medium", "share_plat", "share_session_id",
            "spm_id_from", "from_source", "from", "source", "timestamp", "ts",
        }
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in tracking]
        cleaned = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )
        return cleaned or url
    except Exception:
        return url


def make_limiter(base_dir: str | Path) -> RateLimiter:
    return RateLimiter(Path(base_dir) / "rate_limit.json")


def load_rate_cfg(source: dict | None) -> dict:
    """从 bridge 配置源提取限频配置 (非硬编码)."""
    if not source:
        return {}
    raw = source.get("rate_limit", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}
