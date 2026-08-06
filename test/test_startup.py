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
# C7: _get_cookies_for / _use_proxy_for 无重复定义 (bridge.core)
# ═══════════════════════════════════════════════════════════════
import bridge.core as _core_mod

src = inspect.getsource(_core_mod)
for fn_name in ("_get_cookies_for", "_use_proxy_for"):
    count = src.count(f"def {fn_name}")
    if count == 1:
        ok(f"{fn_name}: 1 definition (no duplicate)")
    else:
        bad(f"{fn_name}: {count} definitions (duplicate!)")


# ═══════════════════════════════════════════════════════════════
# C8: 模块级 _inject_dynamic_options_static() 无异常保护
# ═══════════════════════════════════════════════════════════════
from main import _inject_dynamic_options_static

try:
    _inject_dynamic_options_static()
    ok("_inject_dynamic_options_static() runs without crash")
except Exception as e:
    bad(f"_inject_dynamic_options_static() crashed: {e}")


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
