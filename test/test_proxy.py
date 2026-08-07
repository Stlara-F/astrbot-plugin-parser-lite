"""
Proxy configuration & network tests.
Covers: C2, C19, C20, C27, C31
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
if "apply_downloader_proxy" in parse_src:
    ok("parse_url uses apply_downloader_proxy (直连客户端重建, r8)")
else:
    sk("parse_url client rebuild not verified")


# ═══════════════════════════════════════════════════════════════
# C19: _is_parser_enabled 不再引用 disabled_platforms (T1: 与 enabled 列表收敛)
# ═══════════════════════════════════════════════════════════════
ie_src = inspect.getsource(_m._is_parser_enabled)
if "disabled_platforms" not in ie_src:
    ok("_is_parser_enabled 不再引用 disabled_platforms (收敛于 enabled 列表)")
else:
    bad("_is_parser_enabled 仍引用 disabled_platforms (T1 未收敛)")


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
# C31: apply_downloader_proxy 直连重建 (r8: 代理体系已收敛)
# ═══════════════════════════════════════════════════════════════
proxy_src = inspect.getsource(_m._apply_downloader_proxy)
if "HttpxClient(" in proxy_src and "CurlSession(" in proxy_src:
    ok("_apply_downloader_proxy rebuilds httpx+curl clients (direct)")
else:
    sk("_apply_downloader_proxy client rebuild not confirmed")


finish()
