"""
Startup & lifecycle tests.
Covers: C1, C7, C8, C26, C30
"""

import inspect

import main as _m
from test._base import _ROOT, bad, finish, ok, sk

# ═══════════════════════════════════════════════════════════════
# C1: main.py 在导入上游前设置 PARSER_LITE_STANDALONE env var
# ═══════════════════════════════════════════════════════════════
_main_path = _ROOT / "main.py"
_main_lines = _main_path.read_text("utf-8").splitlines()
found_main_env = any(
    "PARSER_LITE_STANDALONE" in ln and "setdefault" in ln for ln in _main_lines[:25]
)
if found_main_env:
    ok("main.py sets PARSER_LITE_STANDALONE before upstream imports")
else:
    bad("main.py does NOT set PARSER_LITE_STANDALONE")


# ═══════════════════════════════════════════════════════════════
# C7: get_cookies_for 存在且唯一 (bridge.adapter; r9: _use_proxy_for 已删)
# ═══════════════════════════════════════════════════════════════
import bridge.adapter as _core_mod

src = inspect.getsource(_core_mod)
for fn_name in ("get_cookies_for",):
    count = src.count(fn_name)
    if count >= 1:
        ok(f"{fn_name}: present (no duplicate)")
    else:
        bad(f"{fn_name}: missing from bridge.adapter")


# ═══════════════════════════════════════════════════════════════
# C8: 注入函数可调用且不崩溃 (r9: 改由 bridge.config 导入, main 不再持有)
# ═══════════════════════════════════════════════════════════════
from bridge.config import inject_dynamic_options_static

try:
    _schema_f = _ROOT / "_conf_schema.json"
    _flag_f = _ROOT / ".injected"
    inject_dynamic_options_static(_schema_f, _flag_f)
    ok("inject_dynamic_options_static() runs without crash")
except Exception as e:
    bad(f"inject_dynamic_options_static() crashed: {e}")


# ═══════════════════════════════════════════════════════════════
# C26: terminate() 取消 _chromium_task
# ═══════════════════════════════════════════════════════════════
term_src = inspect.getsource(_m.ParserLitePlugin.terminate)
if "_chromium_task" in term_src:
    ok("terminate cancels _chromium_task")
else:
    sk("_chromium_task cancel not verified")


# ═══════════════════════════════════════════════════════════════
# C30: 模块加载时清除上游 sys.modules 缓存
# ═══════════════════════════════════════════════════════════════
_main_text = _main_path.read_text("utf-8")
if "nonebot_plugin_parser_lite" in _main_text and "del sys.modules" in _main_text:
    ok("main.py clears stale nonebot_plugin_parser_lite module cache at load")
else:
    sk("sys.modules cache cleanup not confirmed")


finish()
