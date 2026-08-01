"""
Shared test infrastructure: path setup, result tracking, exit handling.
"""
import os
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
_src = str(_ROOT / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

results = {"PASS": 0, "FAIL": 0, "SKIP": 0}
failures: list[str] = []


def ok(msg: str):
    results["PASS"] += 1


def bad(msg: str):
    results["FAIL"] += 1
    failures.append(msg)


def sk(msg: str):
    results["SKIP"] += 1


def finish():
    import sys
    sys.exit(0 if results["FAIL"] == 0 else 1)
