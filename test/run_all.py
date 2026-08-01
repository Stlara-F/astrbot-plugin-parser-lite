"""
Test runner: auto-discovers and runs all test modules under test/.
Usage:
  py -3 test/run_all.py                  # quick: regression + smoke + URL detection
  py -3 test/run_all.py --online         # full: + online parse tests
  py -3 test/run_all.py --smoke          # fast: only regression + smoke checks
  py -3 test/run_all.py --verbose        # show tracebacks on failure
"""
import argparse
import asyncio
import importlib
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

ap = argparse.ArgumentParser()
ap.add_argument("--online", action="store_true", help="Run online parse tests (requires network)")
ap.add_argument("--smoke", action="store_true", help="Smoke-only: regression + encoding + git integrity")
ap.add_argument("--verbose", "-v", action="store_true")
args = ap.parse_args()

TEST_DIR = Path(__file__).resolve().parent
modules = sorted(p.stem for p in TEST_DIR.glob("test_*.py") if p.stem != "test_parsers")

results: dict[str, dict] = {}
start = time.time()

print("=" * 60)  # noqa: T201
if args.smoke:
    print("Smoke Test Suite")  # noqa: T201
else:
    print(f"Test Suite ({len(modules) + 1} modules)")  # noqa: T201
print("=" * 60)  # noqa: T201

for mod_name in modules:
    t0 = time.time()
    print(f"\n-- {mod_name} --")  # noqa: T201
    try:
        importlib.import_module(f"test.{mod_name}")
        elapsed = time.time() - t0
        results[mod_name] = {"status": "PASS", "time": elapsed}
    except SystemExit as e:
        elapsed = time.time() - t0
        results[mod_name] = {"status": "PASS" if e.code == 0 else "FAIL", "time": elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        results[mod_name] = {"status": f"ERROR: {e}", "time": elapsed}
        if args.verbose:
            import traceback as _tb
            _tb.print_exc()

if not args.smoke:
    print("\n-- test_parsers --")  # noqa: T201
    t0 = time.time()
    import test.test_parsers as tp

    orig_argv = sys.argv
    sys.argv = ["test_parsers"]
    if args.online:
        sys.argv.append("--online")
    try:
        exit_code = asyncio.run(tp.main())
        elapsed = time.time() - t0
        results["test_parsers"] = {"status": "WARN" if exit_code != 0 else "PASS", "time": elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        results["test_parsers"] = {"status": f"ERROR: {e}", "time": elapsed}
    finally:
        sys.argv = orig_argv

print(f"\n{'=' * 60}")  # noqa: T201
print("Summary")  # noqa: T201
print(f"{'=' * 60}")  # noqa: T201
total = len(results)
passed = sum(1 for r in results.values() if r["status"] == "PASS")
failed = sum(1 for r in results.values() if r["status"] != "PASS" and r["status"] != "WARN")
warned = sum(1 for r in results.values() if r["status"] == "WARN")
for name, r in results.items():
    status = r["status"]
    icon = "v" if status == "PASS" else ("!" if status == "WARN" else "x")
    print(f"  [{icon}] {name}: {status} ({r['time']:.1f}s)")  # noqa: T201
elapsed = time.time() - start
print(f"\n{passed}/{total} passed, {failed} failed, {warned} warnings ({elapsed:.1f}s total)")  # noqa: T201
print(f"{'=' * 60}")  # noqa: T201
sys.exit(0 if failed == 0 else 1)
