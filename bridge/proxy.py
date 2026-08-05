"""代理桥接: DOWNLOADER 客户端代理注入 (扩展层, 与上游解耦).

- monkey-patch DOWNLOADER.client 的 httpx/curl_cffi 实例 (库不读环境变量)
- 平台级走代理: platforms[].proxy 勾选 (默认直连)
- 代理失败回退直连 (附加配置不阻断解析)
"""

from __future__ import annotations

import asyncio

from bridge.cfg import read_cfg
from bridge.context import BridgeConfig, up_downloader

# 代理协议轮询列表 (curl_cffi 全支持, httpx 需 httpx[socks])
PROXY_PROTOCOLS = ("http://", "https://", "socks5://", "socks5h://")

_last_proxy: str | None = None


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


def read_proxy_config() -> str:
    """读取全局代理配置 (单一来源: read_cfg)."""
    import logging

    px = read_cfg(BridgeConfig._source or {}, "plite_http_proxy", "") or ""
    logging.getLogger("nonebot_plugin_parser_lite").info(
        f"[ParserLite] proxy config: pyx={px or '<not set>'}")
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
    except Exception:
        pass  # curl_cffi 未初始化会话关闭会抛 TypeError — 忽略


def apply_downloader_proxy(proxy_url: str):
    """将代理注入 DOWNLOADER 的 httpx/curl_cffi 客户端.

    proxy 未变但 client 已关闭 (插件更新重载后) → 强制重建.
    """
    global _last_proxy
    proxy_url = resolve_proxy_url(proxy_url)
    client = up_downloader().client
    if proxy_url == (_last_proxy or "") and not client_closed(client):
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
        f"[ParserLite] downloader proxy: {proxy_url or 'disabled'}")


# ── 平台级代理决策 ──────────────────────────────────────────────────────────

def platform_cfg(platform: str) -> dict:
    """每平台独立配置 (platforms template_list 单一事实来源)."""
    try:
        pfm = (BridgeConfig._source or {}).get("platforms", []) or []
        if isinstance(pfm, list):
            for item in pfm:
                if isinstance(item, dict) and str(item.get("platform", "")).lower() == platform.lower():
                    return item
        elif isinstance(pfm, dict):
            return pfm.get(platform, {}) or {}
    except Exception:
        pass
    return {}


def load_parsers_config() -> dict:
    """读取 parsers 配置 (仅旧配置迁移期兼容, 新配置统一 platforms)."""
    try:
        p = (BridgeConfig._source or {}).get("parsers", {}) or {}
        if isinstance(p, dict):
            inner = p.get("items", p) if isinstance(p.get("items"), dict) else p
            if isinstance(inner, dict):
                return inner
    except Exception:
        pass
    return {}


def use_proxy_for(platform: str) -> bool:
    """平台是否走代理: platforms[].proxy 勾选 (默认直连)."""
    try:
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
    """平台 Cookie: platforms[].cookies (单一来源)."""
    import json as _json

    try:
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
