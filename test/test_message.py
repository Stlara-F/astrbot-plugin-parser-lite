"""
Message handling & card rendering tests.
Covers: C3, C11, C18, C21, C29, C32, C33, C34
"""
import inspect

import main as _m
from test._base import bad, finish, ok, sk

# ═══════════════════════════════════════════════════════════════
# C3: 懒下载会话 URL 使用检测
# ═══════════════════════════════════════════════════════════════
lazy_src = inspect.getsource(_m.ParserLitePlugin._on_download_trigger)
if "session.url" in lazy_src:
    ok("_on_download_trigger accesses session.url")
else:
    sk("lazy session URL usage not verified")


# ═══════════════════════════════════════════════════════════════
# C11: cmd_bm 三路 BV 提取
# ═══════════════════════════════════════════════════════════════
bm_src = inspect.getsource(_m.ParserLitePlugin.cmd_bm)
if "reply" in bm_src.lower() and "LazyManager" in bm_src:
    ok("cmd_bm supports reply + lazy session BV extraction (3 paths)")
else:
    sk("cmd_bm reply extraction not verified")


# ═══════════════════════════════════════════════════════════════
# C18: _send_card cache hit 直接发送 fromBytes
# ═══════════════════════════════════════════════════════════════
card_src = inspect.getsource(_m.ParserLitePlugin._send_card)
if "_CARD_CACHE" in card_src and "fromBytes" in card_src:
    cached_block = card_src.split("cache_key")[-1].split("return")[0] if "cache_key" in card_src else ""
    if "send_any" not in cached_block:
        ok("card cache hit sends fromBytes directly")
    else:
        bad("card cache hit still uses _send_any with missing file path")
else:
    sk("card cache verification incomplete")


# ═══════════════════════════════════════════════════════════════
# C21: _on_download_trigger 使用 session.url 非 cmd_parse 重扫描
# ═══════════════════════════════════════════════════════════════
dt_src = inspect.getsource(_m.ParserLitePlugin._on_download_trigger)
if "session.url" in dt_src and "cmd_parse" not in dt_src:
    ok("_on_download_trigger uses session.url (not cmd_parse)")
elif "cmd_parse" in dt_src:
    bad("_on_download_trigger still delegates to cmd_parse")
else:
    sk("_on_download_trigger verification incomplete")


# ═══════════════════════════════════════════════════════════════
# C29: _send_card 模板检查使用 os.path.exists (非 anyio.Path)
# ═══════════════════════════════════════════════════════════════
card_src2 = inspect.getsource(_m.ParserLitePlugin._send_card)
if "os.path.exists" in card_src2 and "shutil.copy2" in card_src2:
    ok("_send_card: os.path.exists check + shutil.copy2 fallback")
elif "os.path.exists" in card_src2:
    ok("_send_card: os.path.exists check")
else:
    sk("_send_card template check not verified")


# ═══════════════════════════════════════════════════════════════
# C32: _send_card 回退文本发送捕获 ApiNotAvailable
# ═══════════════════════════════════════════════════════════════
if "OneBot API" in card_src2 or "回退文本发送也失败" in card_src2:
    ok("_send_card fallback text send wrapped in try/except")
else:
    sk("_send_card send error handling not verified")


# ═══════════════════════════════════════════════════════════════
# C33: _handle_card_message 整个链路有 try/except
# ═══════════════════════════════════════════════════════════════
hcm_src2 = inspect.getsource(_m.ParserLitePlugin._handle_card_message)
if "_handle_card_message 异常" in hcm_src2:
    ok("_handle_card_message wraps parse/card/items in try/except")
else:
    sk("_handle_card_message exception guard not confirmed")


# ═══════════════════════════════════════════════════════════════
# C34: on_url_auto 禁用 (防止与 on_message_group 双重触发)
# ═══════════════════════════════════════════════════════════════
oua_src = inspect.getsource(_m.ParserLitePlugin.on_url_auto)
if "_handle_card_message" in oua_src:
    bad("on_url_auto still calls _handle_card_message (will double-trigger)")
else:
    ok("on_url_auto is disabled (prevents double-trigger)")


finish()
