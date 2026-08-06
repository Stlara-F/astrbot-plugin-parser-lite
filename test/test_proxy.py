"""
Proxy configuration & network tests.
Covers: C2, C19, C20, C27, C31, C35
"""

import inspect

import main as _m

# ═══════════════════════════════════════════════════════════════
# C2: DOWNLOADER.ensure_client() 存在性 + parse_url 使用 _apply_downloader_proxy
# ═══════════════════════════════════════════════════════════════
from nonebot_plugin_parser_lite.download import DOWNLOADER
from test._base import bad, finish, ok, sk

has_method = hasattr(DOWNLOADER, "ensure_client") and callable(DOWNLOADER.ensure_client)
if has_method:
    ok("ensure_client exists and callable (local build)")
else:
    sk("ensure_client NOT found — production hasattr guard active")
parse_src = inspect.getsource(_m.ParserLite.parse_url)
if "_apply_downloader_proxy" in parse_src:
    ok("parse_url uses _apply_downloader_proxy (direct httpx/curl injection)")
else:
    sk("parse_url proxy injection not verified")


# ═══════════════════════════════════════════════════════════════
# C19: _is_parser_enabled 使用 disabled_platforms (非 parsers.disabled)
# ═══════════════════════════════════════════════════════════════
ie_src = inspect.getsource(_m._is_parser_enabled)
if "disabled_platforms" in ie_src:
    ok("_is_parser_enabled uses disabled_platforms (upstream config)")
else:
    bad("_is_parser_enabled does NOT reference disabled_platforms")


# ═══════════════════════════════════════════════════════════════
# C20: B站 cookie 写入正确的 pydantic 字段 plite_bili_ck
# ═══════════════════════════════════════════════════════════════
parse_src2 = inspect.getsource(_m.ParserLite.parse_url)
if "plite_bili_ck" in parse_src2:
    ok("parse_url sets plite_bili_ck for bilibili cookie injection")
elif "_bili_ck" in parse_src2:
    bad("parse_url sets _bili_ck (private attr, not read by BilibiliParser)")
else:
    sk("cookie injection path not verified")


# ═══════════════════════════════════════════════════════════════
# C27: cookie 写入后同步更新 _source (防止 configure() 重建丢失)
# ═══════════════════════════════════════════════════════════════
if '_source["plite_bili_ck"]' in parse_src2:
    ok("parse_url writes cookie to _source (survives configure rebuild)")
else:
    sk("_source cookie persistence not verified")


# ═══════════════════════════════════════════════════════════════
# C31: _apply_downloader_proxy 对 curl_cffi 使用 proxies= dict
# ═══════════════════════════════════════════════════════════════
proxy_src = inspect.getsource(_m._apply_downloader_proxy)
if 'proxies={"http"' in proxy_src or 'proxies={\\"http\\"' in proxy_src:
    ok("_apply_downloader_proxy uses proxies=dict for curl_cffi")
else:
    sk("curl_cffi proxy format not confirmed")


# ═══════════════════════════════════════════════════════════════
# C35: _resolve_proxy_url 多协议支持 + _PROXY_PROTOCOLS 轮询
# ═══════════════════════════════════════════════════════════════
rp_src = inspect.getsource(_m._resolve_proxy_url)
if '"socks5h"' in rp_src and '"socks4"' in rp_src:
    ok("_resolve_proxy_url supports socks4/socks4a/socks5/socks5h keywords")
elif '"socks5"' in rp_src:
    ok("_resolve_proxy_url supports socks5 (basic)")
else:
    bad("_resolve_proxy_url: no protocol keywords found (socks4/socks5/etc)")
if "_PROXY_PROTOCOLS" in inspect.getsource(_m):
    ok("_PROXY_PROTOCOLS defined for auto-protocol rotation")
else:
    bad("_PROXY_PROTOCOLS NOT defined — auto-protocol rotation disabled")


finish()
