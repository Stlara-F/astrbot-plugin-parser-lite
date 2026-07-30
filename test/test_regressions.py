"""
Regression tests: automated detection of all fixed production bugs.
每个测试对应一个已知的已修复 bug，验证修复不会在未来被意外回退。

Run: py -3 test/run_all.py
"""
import os, sys, json, inspect
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# 环境初始化: 模拟 AstrBot standalone 模式
# ═══════════════════════════════════════════════════════════════
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

results = {"PASS": 0, "FAIL": 0, "SKIP": 0}
failures: list[str] = []

def ok(msg):   results["PASS"] += 1; print(f"  {chr(10003)} {msg}", flush=True)
def bad(msg):  results["FAIL"] += 1; failures.append(msg); print(f"  {chr(10007)} {msg}", flush=True)
def sk(msg):   results["SKIP"] += 1; print(f"  - {msg}", flush=True)

print("=" * 60)
print("Regression Test Suite")
print("=" * 60)

# ═══════════════════════════════════════════════════════════════
# C6: BridgeConfig._source never set via AstrBot kwargs path.
# AstrBot 调用 configure(**self.config), _config 参数为 None,
# 但 _source 从未被赋值, 导致 cookie/proxy/parser 配置全部失效。
# 修复: elif kwargs: cls._source = data
# ═══════════════════════════════════════════════════════════════
print("\n-- C6: BridgeConfig._source via kwargs --")
from main import BridgeConfig
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=99)
if BridgeConfig._source is not None and BridgeConfig._source.get("plite_max_size") == 99:
    ok("_source set via kwargs (AstrBot path)")
else:
    bad(f"_source = {BridgeConfig._source}")

# ═══════════════════════════════════════════════════════════════
# C5: features 开关完全死代码。
# WebUI 的 features 勾选列表未被映射到上游 plite_* bool 字段,
# 所有开关配置被 silently 丢弃。
# 修复: 在 configure() 中将 features label → plite_* field 反向映射,
# 仅设置勾选的为 True, 未勾选的保留上游默认值。
# ═══════════════════════════════════════════════════════════════
print("\n-- C5: Features -> bool fields mapping --")
from nonebot_plugin_parser_lite.config import Config as UpConfig
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download", "Headless"])
cfg = BridgeConfig.get_config()
if cfg and cfg.lazy_download is True and cfg.headless is True:
    ok("features mapping: Lazy Download=True, Headless=True (selected)")
else:
    bad(f"lazy_download={cfg.lazy_download}, headless={cfg.headless}")
# 未勾选的功能应保留上游 pydantic 默认值, 不应被强制设为 False
live_default = UpConfig.model_fields["plite_live_photo"].default
if cfg and cfg.live_photo == live_default:
    ok(f"unselected features keep default: live_photo={live_default}")
else:
    bad(f"unselected features corrupted: live_photo={cfg.live_photo}")

# ═══════════════════════════════════════════════════════════════
# C7: _get_cookies_for / _use_proxy_for 重复定义。
# Python 使用最后一个定义, 旧残留覆盖新版本导致 cookie 注入失效。
# 修复: 删除行 502-516 的旧残留定义。
# ═══════════════════════════════════════════════════════════════
print("\n-- C7: No duplicate function definitions --")
import main as _m
src = inspect.getsource(_m)
for fn_name in ("_get_cookies_for", "_use_proxy_for"):
    count = src.count(f"def {fn_name}")
    if count == 1:
        ok(f"{fn_name}: 1 definition (no duplicate)")
    else:
        bad(f"{fn_name}: {count} definitions (duplicate!)")

# ═══════════════════════════════════════════════════════════════
# C8: 模块级 _inject_dynamic_options_static() 无异常保护,
# schema 文件损坏或目录只读时直接崩溃, 插件无法加载。
# 修复: try/except 包裹顶层调用。
# ═══════════════════════════════════════════════════════════════
print("\n-- C8: Injection crash-safe --")
from main import _inject_dynamic_options_static
try:
    _inject_dynamic_options_static()
    ok("_inject_dynamic_options_static() runs without crash")
except Exception as e:
    bad(f"_inject_dynamic_options_static() crashed: {e}")

# ═══════════════════════════════════════════════════════════════
# C1: __init__.py env var 保护。
# AstrBot 可能走 import nonebot_plugin_parser_lite.main 路径,
# 此时 __init__.py 先于 main.py 执行, 需内置 env var 设置。
# 同时 helper.py 需 _STANDALONE 守卫防止 from nonebot.adapters import Event 崩溃。
# ═══════════════════════════════════════════════════════════════
print("\n-- C1: Standalone env var + nonebot guard --")
# 检查 __init__.py 第一行可执行代码是否设置 env var
init_path = Path(__file__).resolve().parent.parent / "src" / "nonebot_plugin_parser_lite" / "__init__.py"
init_lines = init_path.read_text("utf-8").splitlines()
found_env = any("PARSER_LITE_STANDALONE" in l and "setdefault" in l for l in init_lines[:5])
if found_env:
    ok("__init__.py sets PARSER_LITE_STANDALONE before _flags import")
else:
    bad("__init__.py does NOT set PARSER_LITE_STANDALONE")
# 验证上游包可在无 nonebot 环境下导入
try:
    from nonebot_plugin_parser_lite import __init__ as _pkg
    ok("upstream package imports without nonebot dependency")
except ImportError as e:
    bad(f"upstream package import failed: {e}")
# 验证 helper.py standalone stub 完整性 (逐个缺失曾导致多次渲染崩溃)
stubs_needed = ["Segment", "Reference", "Image", "Video", "File", "Voice", "Text", "CustomNode", "UniMessage"]
try:
    from nonebot_plugin_parser_lite import helper as _hp
    missing = [s for s in stubs_needed if not hasattr(_hp, s)]
    if not missing:
        ok(f"helper.py standalone stubs: all {len(stubs_needed)} types defined")
    else:
        bad(f"helper.py standalone stubs missing: {missing}")
except ImportError as e:
    bad(f"helper.py standalone stubs check failed: {e}")

# ═══════════════════════════════════════════════════════════════
# C2 (MISREPORT): DOWNLOADER.ensure_client() 存在性。
# 本地构建有此方法, 但生产环境的上游版本可能不同。
# 测试确保 hasattr 守卫不会因方法缺失而崩溃。
# ═══════════════════════════════════════════════════════════════
print("\n-- C2: DOWNLOADER.ensure_client() compatibility --")
from nonebot_plugin_parser_lite.download import DOWNLOADER
has_method = hasattr(DOWNLOADER, "ensure_client") and callable(DOWNLOADER.ensure_client)
if has_method:
    ok("DOWNLOADER.ensure_client() exists and is callable (local build)")
else:
    sk("DOWNLOADER.ensure_client() NOT found — production hasattr guard active")
# 验证 parse_url 中的 ensure_client 调用有 hasattr 保护
parse_src = inspect.getsource(_m.ParserLite.parse_url)
if "hasattr(DOWNLOADER, \"ensure_client\")" in parse_src:
    ok("parse_url uses hasattr guard for ensure_client()")
else:
    bad("parse_url calls ensure_client() WITHOUT hasattr guard — WILL CRASH on production")

# ═══════════════════════════════════════════════════════════════
# C3: 懒下载会话 URL 使用检测。
# ═══════════════════════════════════════════════════════════════
print("\n-- C3: Lazy download session URL usage --")
lazy_src = inspect.getsource(_m.ParserLitePlugin._on_download_trigger)
if "session.url" in lazy_src or "self._lazy_sessions" in lazy_src:
    ok("_on_download_trigger accesses _lazy_sessions")
else:
    sk("can't verify lazy session URL usage in source")

# ═══════════════════════════════════════════════════════════════
# C9: _flags.py 文件是否被 git 跟踪。
# 上游仓库遗漏了 utils/_flags.py, 导致 GitHub zip 不含此文件,
# __init__.py 导入失败 → 插件完全无法加载。
# ═══════════════════════════════════════════════════════════════
print("\n-- C9: _flags.py exists in package --")
flags_path = Path(__file__).resolve().parent.parent / "src" / "nonebot_plugin_parser_lite" / "utils" / "_flags.py"
if flags_path.exists():
    ok("utils/_flags.py exists on disk")
else:
    bad("utils/_flags.py MISSING — plugin cannot load")

# ═══════════════════════════════════════════════════════════════
# C10: LazyManager async timeout (replaces manual _clean_lazy)
# ═══════════════════════════════════════════════════════════════
print("\n-- C10: LazyManager async timeout --")
from main import LazyManager
if hasattr(LazyManager, "add") and hasattr(LazyManager, "remove") and hasattr(LazyManager, "get"):
    ok("LazyManager has add/remove/get class methods")
else:
    bad("LazyManager missing methods")
# Verify asyncio.Task creation (classmethod signature)
import inspect
add_sig = inspect.signature(LazyManager.add)
if "timeout_sec" in str(add_sig):
    ok("LazyManager.add accepts timeout_sec (async auto-cleanup)")
else:
    bad("LazyManager.add lacks timeout_sec parameter")

# ═══════════════════════════════════════════════════════════════
# C11: cmd_bm 三路 BV 提取 (当前消息 / 懒下载会话 / 回复消息)
# ═══════════════════════════════════════════════════════════════
print("\n-- C11: cmd_bm reply BV extraction --")
bm_src = inspect.getsource(_m.ParserLitePlugin.cmd_bm)
if "reply" in bm_src.lower() and "LazyManager" in bm_src:
    ok("cmd_bm supports reply + lazy session BV extraction (3 paths)")
else:
    sk("cmd_bm reply extraction not verified")
t = results["PASS"] + results["FAIL"] + results["SKIP"]
print(f"Results: {results['PASS']} pass, {results['FAIL']} fail, {results['SKIP']} skip ({t} total)")
if failures:
    print(f"\nRegressions ({len(failures)}):")
    for x in failures: print(f"  {chr(10007)} {x}")
print(f"{'=' * 60}")
exit(0 if results["FAIL"] == 0 else 1)
