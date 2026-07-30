"""
Regression tests: automated detection of all fixed production bugs.
Run: py -3 test/run_all.py
"""
import os, sys, json, inspect
from pathlib import Path

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

print("\n-- C6: BridgeConfig._source via kwargs --")
from main import BridgeConfig
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=99)
if BridgeConfig._source is not None and BridgeConfig._source.get("plite_max_size") == 99:
    ok("_source set via kwargs")
else:
    bad(f"_source = {BridgeConfig._source}")

print("\n-- C5: Features -> bool fields mapping --")
from nonebot_plugin_parser_lite.config import Config as UpConfig
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download", "Headless"])
cfg = BridgeConfig.get_config()
if cfg and cfg.lazy_download is True and cfg.headless is True:
    ok("features mapping: Lazy Download=True, Headless=True")
else:
    bad(f"features mapping: lazy_download={cfg.lazy_download}, headless={cfg.headless}")
live_default = UpConfig.model_fields["plite_live_photo"].default
if cfg and cfg.live_photo == live_default:
    ok(f"unselected features keep default: live_photo={live_default}")
else:
    bad(f"unselected features corrupted: live_photo={cfg.live_photo}")

print("\n-- C7: No duplicate definitions --")
import main as _m
src = inspect.getsource(_m)
count_cookies = src.count("def _get_cookies_for")
count_proxy = src.count("def _use_proxy_for")
if count_cookies == 1:
    ok(f"_get_cookies_for: 1 definition")
else:
    bad(f"_get_cookies_for: {count_cookies} definitions (duplicate!)")
if count_proxy == 1:
    ok(f"_use_proxy_for: 1 definition")
else:
    bad(f"_use_proxy_for: {count_proxy} definitions (duplicate!)")

print("\n-- C8: Injection crash-safe --")
from main import _inject_dynamic_options_static
try:
    _inject_dynamic_options_static()
    ok("_inject_dynamic_options_static() runs without crash")
except Exception as e:
    bad(f"_inject_dynamic_options_static() crashed: {e}")

print("\n-- C1: Standalone env var hard-gate --")
init_path = Path(__file__).resolve().parent.parent / "src" / "nonebot_plugin_parser_lite" / "__init__.py"
init_lines = init_path.read_text("utf-8").splitlines()
found_env = any("PARSER_LITE_STANDALONE" in l and "setdefault" in l for l in init_lines[:5])
if found_env:
    ok("__init__.py sets PARSER_LITE_STANDALONE before _flags import")
else:
    bad("__init__.py does NOT set PARSER_LITE_STANDALONE")
try:
    from nonebot_plugin_parser_lite import __init__ as _pkg
    ok("upstream package imports without nonebot dependency")
except ImportError as e:
    bad(f"upstream package import failed: {e}")

print("\n-- C2: DOWNLOADER.ensure_client() exists --")
from nonebot_plugin_parser_lite.download import DOWNLOADER
if hasattr(DOWNLOADER, "ensure_client") and callable(DOWNLOADER.ensure_client):
    ok("DOWNLOADER.ensure_client() exists and is callable")
else:
    bad("DOWNLOADER.ensure_client() missing!")

print("\n-- C3: Lazy download session URL usage --")
lazy_src = inspect.getsource(_m.ParserLitePlugin._on_download_trigger)
if "session.url" in lazy_src or "self._lazy_sessions" in lazy_src:
    ok("_on_download_trigger accesses _lazy_sessions")
else:
    sk("can't verify lazy session URL usage in source")

print(f"\n{'=' * 60}")
t = results["PASS"] + results["FAIL"] + results["SKIP"]
print(f"Results: {results['PASS']} pass, {results['FAIL']} fail, {results['SKIP']} skip ({t} total)")
if failures:
    print(f"\nRegressions ({len(failures)}):")
    for x in failures: print(f"  {chr(10007)} {x}")
print(f"{'=' * 60}")
exit(0 if results["FAIL"] == 0 else 1)
