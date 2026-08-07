"""DOWNLOADER 客户端桥接 (扩展层, 与上游解耦).

T2: 代理体系已收敛为直连 — 全局代理/规则代理配置移除,
apply_downloader_proxy 仅承担插件重载后 DOWNLOADER 客户端重建职责.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from bridge.cfg import global_source
from bridge.context import up_downloader

_logger = logging.getLogger("nonebot_plugin_parser_lite")


def _log_cfg_fallback(exc: Exception) -> None:
    """统一"配置读取回退"日志 (debug 级, 不刷屏)."""
    _logger.debug(f"[ParserLite] 配置读取回退: {exc}")


_PROXY_LOCK = threading.Lock()


def client_closed(client) -> bool:
    """检测 DOWNLOADER 客户端是否已关闭 (插件重载/terminate 后残留)."""
    for attr in ("_httpx", "_curl"):
        s = getattr(client, attr, None)
        if s is None:
            return True
        if getattr(s, "is_closed", False):
            return True
        if getattr(s, "_curl", None) is None and hasattr(s, "_curl"):
            return True
    return False


async def _safe_close(_c):
    try:
        await _c.aclose() if hasattr(_c, "aclose") else _c.close()
    except (TypeError, RuntimeError, asyncio.CancelledError):
        pass  # curl_cffi 未初始化会话关闭会抛 TypeError — 忽略 (B18: 收窄捕获)


def apply_downloader_proxy(proxy_url: str = ""):
    """重建 DOWNLOADER 客户端 (直连, T2: 代理配置已移除).

    并发安全: threading.Lock 互斥重建 (避免 client 抖动/重复替换).
    早期返回: 客户端存活 (None 视为未初始化 → 需重建).
    :param proxy_url: 兼容旧签名, 恒忽略 (代理体系已收敛直连)
    """
    with _PROXY_LOCK:
        client = up_downloader().client
        _httpx = getattr(client, "_httpx", None)
        _curl = getattr(client, "_curl", None)
        _httpx_alive = _httpx is not None and not getattr(_httpx, "is_closed", False)
        _curl_alive = _curl is not None and not getattr(_curl, "is_closed", False)
        if _httpx_alive and _curl_alive:
            return
        from curl_cffi import AsyncSession as CurlSession
        from httpx import AsyncClient as HttpxClient
        from httpx import Timeout

        # 调度旧客户端关闭 (插件重载后残留客户端需释放)
        for _old_attr in ("_httpx", "_curl"):
            _old = getattr(client, _old_attr, None)
            if _old is not None:
                try:
                    asyncio.get_running_loop().create_task(_safe_close(_old))
                except RuntimeError:
                    # B3: 无运行中 loop → 同步 close 兜底 (释放连接池, 不静默丢弃)
                    try:
                        _old.close()
                    except (TypeError, RuntimeError):
                        pass
        client._httpx = HttpxClient(
            verify=False, follow_redirects=True, timeout=Timeout(timeout=15)
        )
        client._curl = CurlSession(
            impersonate="chrome146", timeout=240, verify=False, allow_redirects=True
        )

    _logger.info("[ParserLite] downloader client rebuilt (direct)")


# ── 平台级决策 (统一勾选列表: platforms.items.{enabled,cookies}) ──

# 平台块惰性缓存: {源哈希: 归一化块}; configure() 更新 _source 后哈希变化自动失效
_PLATFORMS_CACHE: dict[str, dict] = {}


def _platforms_block() -> dict:
    """读取 platforms 配置块 (新格式 object / 旧格式 template_list 迁移).

    AstrBot 生成配置时会把 object 类型配置展平为顶层键:
    schema 形式 {"items": {"enabled": [...]}} → 配置形式 {"enabled": [...]}
    惰性缓存: 配置源哈希不变则复用解析结果 (热路径免重复归一化).
    """
    import hashlib

    _src = global_source()
    # R8: 仅 platforms 段参与哈希 (非 platforms 键变更不误失效缓存)
    _h = hashlib.md5(repr(_src.get("platforms")).encode()).hexdigest()
    _cached = _PLATFORMS_CACHE.get(_h)
    if _cached is not None:
        return _cached
    try:
        pfm = _src.get("platforms", {}) or {}
        if isinstance(pfm, dict):
            if "enabled" in pfm or "cookies" in pfm:
                _block = {"items": pfm}  # AstrBot 展平形态 → 归一化
            else:
                _block = pfm
        elif isinstance(pfm, list):  # 旧 27 模板格式 → 模拟块
            _block = {"items": {}, "_legacy_list": pfm}
        else:
            _block = {}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
        _block = {}
    _PLATFORMS_CACHE[_h] = _block
    return _block


def platform_cfg(platform: str) -> dict:
    """平台配置 (旧 27 模板迁移兼容: enable/cookies; T2: proxy 键已剥离).

    新格式 (items.enabled/cookies) 由 enabled_platforms/cookies_entries 统一读取.
    """
    try:
        _blk = _platforms_block()
        _items = _blk.get("items", {}) if isinstance(_blk, dict) else {}
        # 新格式: 勾选列表内联读取
        if _items:
            _ret = {}
            _enabled = _items.get("enabled", [])
            if isinstance(_enabled, list) and _enabled:
                _ret["enable"] = platform.lower() in _value_set(_enabled)
            # T2: proxied (规则代理) 已移除
            for _ck in _items.get("cookies", []) or []:
                if (
                    isinstance(_ck, dict)
                    and str(_ck.get("platform", "")).lower() == platform.lower()
                ):
                    _ret["cookies"] = str(_ck.get("cookie", "") or "")
                    break
            return _ret
        # 旧格式: template_list / dict (proxy/use_proxy 键剥离)
        _legacy = _blk.get("_legacy_list")
        if isinstance(_legacy, list):
            for item in _legacy:
                if (
                    isinstance(item, dict)
                    and str(item.get("platform", "")).lower() == platform.lower()
                ):
                    return {
                        k: v for k, v in item.items() if k not in ("proxy", "use_proxy")
                    }
        elif isinstance(_blk, dict):
            _legacy_dict = _blk.get(platform, {}) or {}
            if isinstance(_legacy_dict, dict):
                return {
                    k: v
                    for k, v in _legacy_dict.items()
                    if k not in ("proxy", "use_proxy")
                }
            return {}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def _value_set(values: list) -> set[str]:
    """勾选列表值归一化: 支持 {"value": str} / 纯字符串."""
    out = set()
    for v in values or []:
        if isinstance(v, dict):
            v = v.get("value", v.get("label", ""))
        if isinstance(v, str):
            out.add(v.strip().lower())
    return out


def enabled_platforms() -> set[str] | None:
    """新格式勾选启用的平台集合 (None = 未配置 → 全部启用)."""
    try:
        _items = _platforms_block().get("items", {})
        _en = _items.get("enabled", [])
        return _value_set(_en) if isinstance(_en, list) and _en else None
    except Exception:
        return None


def cookies_entries() -> list[dict]:
    """新格式 cookie 条目列表 [{platform, cookie}]."""
    try:
        _items = _platforms_block().get("items", {})
        _ck = _items.get("cookies", [])
        return [
            e
            for e in (_ck or [])
            if isinstance(e, dict) and e.get("platform") and e.get("cookie")
        ]
    except Exception:
        return []


def sync_cookies_to_upstream() -> None:
    """将新格式 cookies 同步至上游 Config 的 plite_<platform>_ck 字段 (动态源).

    仅同步源码声明了 _ck 字段的平台; 无 astrbot/无上游时静默跳过.
    """
    try:
        from bridge.context import up_config

        _cfg = up_config()
        _sync = False
        for entry in cookies_entries():
            _fname = f"plite_{str(entry['platform']).lower()}_ck"
            if (
                _fname in _cfg.model_fields
                and getattr(_cfg, _fname, None) != entry["cookie"]
            ):
                setattr(_cfg, _fname, entry["cookie"])
                _sync = True
        if _sync:
            _logger.info("[ParserLite] cookies synced to upstream config")
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)


def load_parsers_config() -> dict:
    """读取 parsers 配置 (仅旧配置迁移期兼容, 新配置统一 platforms)."""
    try:
        p = (global_source()).get("parsers", {}) or {}
        if isinstance(p, dict):
            inner = p.get("items", p) if isinstance(p.get("items"), dict) else p
            if isinstance(inner, dict):
                return inner
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def get_cookies_for(platform: str) -> dict:
    """平台 Cookie: platforms.items.cookies 条目 (新格式, 自动同步上游).

    旧格式兼容: 27 模板 platforms[].cookies / parsers.items.cookies (废弃).
    """
    import json as _json

    try:
        for entry in cookies_entries():
            if str(entry.get("platform", "")).lower() == platform.lower():
                ck = str(entry.get("cookie", "") or "").strip()
                if ck:
                    return {"Cookie": ck}
        _pc = platform_cfg(platform)
        _ck = (_pc.get("cookies", "") or "").strip()
        if _ck:
            return {"Cookie": _ck}
        # 迁移期兼容: parsers.items.cookies (废弃)
        raw = load_parsers_config().get("cookies", "{}")
        if isinstance(raw, list):
            for entry in raw:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("platform", "")).lower() == platform.lower()
                ):
                    ck = str(entry.get("cookie", "") or "").strip()
                    if ck:
                        return {"Cookie": ck}
            return {}
        cookies = (
            _json.loads(raw)
            if isinstance(raw, str)
            else (raw if isinstance(raw, dict) else {})
        )
        ck = cookies.get(platform, "").strip()
        if ck:
            return {"Cookie": ck}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def build_feature_table() -> dict:
    """动态特征表: URL关键词 → 解析器名 (O(1) 路由, 0 硬编码)."""
    import inspect as _inspect
    import re as _re

    from bridge.context import up_base_parser

    table: dict[str, str] = {}
    for cls in up_base_parser().get_all_subclass():
        name = cls.__name__
        for _, method in _inspect.getmembers(cls, _inspect.isfunction):
            kp = getattr(method, "_key_patterns", None)
            if not kp:
                continue
            for keyword, _pattern, _params in kp:
                if _re.search(r"[\\^$*+?{}()\[\]|]", keyword):
                    continue
                if len(keyword) >= 2:
                    table[keyword] = name
    return table
