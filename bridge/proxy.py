"""代理桥接: DOWNLOADER 客户端代理注入 (扩展层, 与上游解耦).

- monkey-patch DOWNLOADER.client 的 httpx/curl_cffi 实例 (库不读环境变量)
- 平台级走代理: platforms[].proxy 勾选 (默认直连)
- 代理失败回退直连 (附加配置不阻断解析)
"""

from __future__ import annotations

import asyncio

from bridge.cfg import global_source, read_cfg
from bridge.context import up_downloader

# 代理协议轮询列表 (curl_cffi 全支持, httpx 需 httpx[socks])
PROXY_PROTOCOLS = ("http://", "https://", "socks5://", "socks5h://")

_last_proxy: str | None = None
import threading

_PROXY_LOCK = threading.Lock()


def resolve_proxy_url(raw: str) -> str:
    """从用户输入解析代理 URL.

    支持完整协议 / 关键词 (socks5 等) / 裸地址 (ip:port 原样返回).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    _lower = raw.lower()
    for _kw in ("socks5h ", "socks5 ", "socks4a ", "socks4 ", "socks "):
        if _lower.startswith(_kw):
            _proto = _kw.strip()
            if _proto == "socks":
                _proto = "socks5"
            return f"{_proto}://{raw[len(_kw):].strip()}"
    return raw


def _mask_proxy(raw: str) -> str:
    """脱敏代理 URL: 仅保留 scheme + host[:port], 隐藏凭证 (user:pass@).

    形如 socks5://user:pass@1.2.3.4:1080 → socks5://1.2.3.4:1080
    """
    raw = (raw or "").strip()
    if not raw:
        return "<not set>"
    try:
        from urllib.parse import urlsplit

        p = urlsplit(raw if "://" in raw else f"http://{raw}")
        netloc = p.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]  # 去凭证
        host = netloc or p.path
        return f"{p.scheme}://{host}" if p.scheme else host
    except Exception:
        return "***"


def read_proxy_config() -> str:
    """读取全局代理配置 (单一来源: read_cfg).

    日志脱敏: 凭证 (user:pass@) 不落日志 (隐私).
    """
    import logging

    px = read_cfg(global_source(), "plite_http_proxy", "") or ""
    logging.getLogger("nonebot_plugin_parser_lite").info(
        f"[ParserLite] proxy config: proxy={_mask_proxy(px)}")
    return px


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


def apply_downloader_proxy(proxy_url: str):
    """将代理注入 DOWNLOADER 的 httpx/curl_cffi 客户端.

    并发安全: threading.Lock 互斥重建 (避免 client 抖动/重复替换).
    早期返回: proxy 未变且 httpx 客户端存活 (None 视为未初始化 → 需重建).
    日志脱敏: 凭证不落日志.
    """
    global _last_proxy
    proxy_url = resolve_proxy_url(proxy_url)
    with _PROXY_LOCK:
        client = up_downloader().client
        _httpx = getattr(client, "_httpx", None)
        _curl = getattr(client, "_curl", None)
        _httpx_alive = _httpx is not None and not getattr(_httpx, "is_closed", False)
        _curl_alive = _curl is not None and not getattr(_curl, "is_closed", False)
        if proxy_url == (_last_proxy or "") and _httpx_alive and _curl_alive:
            return
        _last_proxy = proxy_url
        from curl_cffi import AsyncSession as CurlSession
        from httpx import AsyncClient as HttpxClient
        from httpx import Timeout
        # 调度旧客户端关闭 (同次解析中可能轮询多个协议, 旧客户端需释放)
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
        if not proxy_url:
            client._httpx = HttpxClient(verify=False, follow_redirects=True,
                                        timeout=Timeout(timeout=15))
            client._curl = CurlSession(impersonate="chrome146", timeout=240,
                                       verify=False, allow_redirects=True)
        else:
            _p = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
            client._httpx = HttpxClient(proxy=_p, verify=False,
                                        follow_redirects=True, timeout=Timeout(timeout=15))
            client._curl = CurlSession(proxies={"http": _p, "https": _p},
                                       impersonate="chrome146",
                                       timeout=240, verify=False, allow_redirects=True)
    import logging
    logging.getLogger("nonebot_plugin_parser_lite").info(
        f"[ParserLite] downloader proxy: {_mask_proxy(proxy_url) or 'disabled'}")


# ── 平台级决策 (统一勾选列表: platforms.items.{enabled,proxied,cookies}) ──

def _platforms_block() -> dict:
    """读取 platforms 配置块 (新格式 object / 旧格式 template_list 迁移).

    AstrBot 生成配置时会把 object 类型配置展平为顶层键:
    schema 形式 {"items": {"enabled": [...]}} → 配置形式 {"enabled": [...]}
    """
    try:
        pfm = (global_source()).get("platforms", {}) or {}
        if isinstance(pfm, dict):
            if "enabled" in pfm or "proxied" in pfm or "cookies" in pfm:
                return {"items": pfm}  # AstrBot 展平形态 → 归一化
            return pfm
        if isinstance(pfm, list):  # 旧 27 模板格式 → 模拟块
            return {"items": {}, "_legacy_list": pfm}
    except Exception:
        pass
    return {}


def platform_cfg(platform: str) -> dict:
    """平台配置 (旧 27 模板迁移兼容: enable/proxy/cookies).

    新格式 (items.enabled/proxied/cookies) 由 enabled_platforms/
    proxied_platforms/cookies_entries 统一读取.
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
            _proxied = _items.get("proxied", [])
            if isinstance(_proxied, list) and _proxied:
                _ret["proxy"] = platform.lower() in _value_set(_proxied)
            for _ck in _items.get("cookies", []) or []:
                if isinstance(_ck, dict) and str(_ck.get("platform", "")).lower() == platform.lower():
                    _ret["cookies"] = str(_ck.get("cookie", "") or "")
                    break
            return _ret
        # 旧格式: template_list / dict
        _legacy = _blk.get("_legacy_list")
        if isinstance(_legacy, list):
            for item in _legacy:
                if isinstance(item, dict) and str(item.get("platform", "")).lower() == platform.lower():
                    return item
        elif isinstance(_blk, dict):
            return _blk.get(platform, {}) or {}
    except Exception:
        pass
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


def proxied_platforms() -> set[str]:
    """新格式勾选走代理的平台集合 (默认空)."""
    try:
        _items = _platforms_block().get("items", {})
        _px = _items.get("proxied", [])
        return _value_set(_px) if isinstance(_px, list) else set()
    except Exception:
        return set()


def cookies_entries() -> list[dict]:
    """新格式 cookie 条目列表 [{platform, cookie}]."""
    try:
        _items = _platforms_block().get("items", {})
        _ck = _items.get("cookies", [])
        return [e for e in (_ck or []) if isinstance(e, dict) and e.get("platform") and e.get("cookie")]
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
            if _fname in _cfg.model_fields and getattr(_cfg, _fname, None) != entry["cookie"]:
                setattr(_cfg, _fname, entry["cookie"])
                _sync = True
        if _sync:
            import logging
            logging.getLogger("nonebot_plugin_parser_lite").info(
                "[ParserLite] cookies synced to upstream config")
    except Exception:
        pass


def load_parsers_config() -> dict:
    """读取 parsers 配置 (仅旧配置迁移期兼容, 新配置统一 platforms)."""
    try:
        p = (global_source()).get("parsers", {}) or {}
        if isinstance(p, dict):
            inner = p.get("items", p) if isinstance(p.get("items"), dict) else p
            if isinstance(inner, dict):
                return inner
    except Exception:
        pass
    return {}


def use_proxy_for(platform: str) -> bool:
    """平台是否走代理: platforms.items.proxied 勾选 (默认直连).

    旧格式兼容: 27 模板 platforms[].proxy / parsers.items.proxied (废弃).
    """
    try:
        _proxied = proxied_platforms()
        if _proxied:
            return platform.lower() in _proxied
        _pc = platform_cfg(platform)
        if "proxy" in _pc:
            return bool(_pc["proxy"])
        if "use_proxy" in _pc:
            return bool(_pc["use_proxy"])
        # 迁移期兼容: parsers.items.proxied (废弃)
        proxied = load_parsers_config().get("proxied", [])
        return platform.lower() in [str(p).lower() for p in proxied]
    except Exception:
        return False


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
                if isinstance(entry, dict) and str(entry.get("platform", "")).lower() == platform.lower():
                    ck = str(entry.get("cookie", "") or "").strip()
                    if ck:
                        return {"Cookie": ck}
            return {}
        cookies = _json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        ck = cookies.get(platform, "").strip()
        if ck:
            return {"Cookie": ck}
    except Exception:
        pass
    return {}


def target_uses_proxy(ordered: list, target: str | None) -> bool:
    """目标平台是否勾选代理 (platforms[].proxy). 默认直连."""
    try:
        if target:
            for cls in ordered:
                if cls.__name__ == target:
                    pn = getattr(getattr(cls, "platform", None), "name", "") or ""
                    return use_proxy_for(str(pn).lower())
            return False
        for cls in ordered:
            pn = getattr(getattr(cls, "platform", None), "name", "") or ""
            if pn and use_proxy_for(str(pn).lower()):
                return True
        return False
    except Exception:
        return False


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
