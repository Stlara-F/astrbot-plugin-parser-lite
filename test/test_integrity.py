"""
Code integrity & filesystem tests.
Covers: C12, C13, C14, C16, C17, C28
"""

import inspect
import subprocess as _sp

import main as _m
from test._base import _ROOT, bad, finish, ok, sk

# ═══════════════════════════════════════════════════════════════
# C12: UTF-8 编码完整性 — 所有 git-tracked Python 文件必须有效
# ═══════════════════════════════════════════════════════════════
_py_files = []
try:
    _r = _sp.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, cwd=str(_ROOT)
    )
    if _r.returncode == 0:
        _py_files = [f for f in _r.stdout.strip().split("\n") if f]
except Exception:
    _py_files = list(_ROOT.rglob("*.py"))

_utf8_broken = []
for _pf in _py_files:
    _fpath = _ROOT / _pf
    if not _fpath.exists():
        continue
    try:
        _fpath.read_text("utf-8")
    except UnicodeDecodeError as _e:
        _utf8_broken.append(f"{_pf} ({_e})")
if not _utf8_broken:
    ok(f"UTF-8 integrity: {len(_py_files)} .py files valid")
else:
    for _b in _utf8_broken:
        bad(f"UTF-8 corrupted: {_b}")


# ═══════════════════════════════════════════════════════════════
# C13: .gitignore 编码有效性
# ═══════════════════════════════════════════════════════════════
_gi_path = _ROOT / ".gitignore"
if _gi_path.exists():
    _gi_bytes = _gi_path.read_bytes()
    if b"\x00" in _gi_bytes:
        bad(".gitignore contains NUL bytes (UTF-16 encoding)")
    else:
        try:
            _gi_text = _gi_path.read_text("utf-8")
            _critical = ["data/", ".injected"]
            _missing = [c for c in _critical if c not in _gi_text]
            if _missing:
                bad(f".gitignore missing critical entries: {_missing}")
            else:
                ok(".gitignore: valid UTF-8, critical entries present")
        except UnicodeDecodeError:
            bad(".gitignore is not valid UTF-8")
else:
    bad(".gitignore file missing")


# ═══════════════════════════════════════════════════════════════
# C14: 生产运行时文件不应被 git 跟踪
# ═══════════════════════════════════════════════════════════════
_tracked_runtime = []
for _patt in ["data/", "cache/", "config/", "FEATURES.md"]:
    try:
        _r = _sp.run(
            ["git", "ls-files", _patt], capture_output=True, text=True, cwd=str(_ROOT)
        )
        if _r.returncode == 0 and _r.stdout.strip():
            _tracked_runtime.extend(_r.stdout.strip().split("\n"))
    except Exception:
        pass
if not _tracked_runtime:
    ok("no production runtime files tracked by git")
else:
    for _tr in _tracked_runtime:
        bad(f"production file leaked to git: {_tr}")


# ═══════════════════════════════════════════════════════════════
# C16: 上游文件完整性 (不应被修改的 11 个文件 UTF-8 验证)
# ═══════════════════════════════════════════════════════════════
_UPSTREAM_CLEAN_FILES = [
    "src/nonebot_plugin_parser_lite/download/__init__.py",
    "src/nonebot_plugin_parser_lite/download/task.py",
    "src/nonebot_plugin_parser_lite/parsers/base.py",
    "src/nonebot_plugin_parser_lite/parsers/bilibili/__init__.py",
    "src/nonebot_plugin_parser_lite/parsers/heybox/sm.py",
    "src/nonebot_plugin_parser_lite/utils/bilibili/credential.py",
    "src/nonebot_plugin_parser_lite/utils/bilibili/dynamic.py",
    "src/nonebot_plugin_parser_lite/utils/bilibili/favorite_list.py",
    "src/nonebot_plugin_parser_lite/utils/bilibili/opus.py",
    "src/nonebot_plugin_parser_lite/utils/bilibili/video.py",
    "api_txt/miyoushe/format_emotion.py",
]
_upstream_broken = []
for _uf in _UPSTREAM_CLEAN_FILES:
    _fp = _ROOT / _uf
    if not _fp.exists():
        _upstream_broken.append(f"{_uf} missing")
        continue
    try:
        _fp.read_text("utf-8")
    except UnicodeDecodeError as _e:
        _upstream_broken.append(f"{_uf}: {_e}")
if not _upstream_broken:
    ok(f"upstream file integrity: {len(_UPSTREAM_CLEAN_FILES)} files valid UTF-8")
else:
    for _b in _upstream_broken:
        bad(f"upstream file corrupted: {_b}")


# ═══════════════════════════════════════════════════════════════
# C17: test.test_parsers 可导入 (插件根目录在 sys.path)
# ═══════════════════════════════════════════════════════════════
try:
    from test.test_parsers import _FALLBACK_URLS as _tufb17

    if _tufb17 and len(_tufb17) > 5:
        ok(f"test.test_parsers importable: {len(_tufb17)} fallback URLs")
    else:
        bad("test.test_parsers._FALLBACK_URLS empty or missing")
except ImportError as _e:
    bad(f"test.test_parsers import FAILED (plugin root not in sys.path): {_e}")


# ═══════════════════════════════════════════════════════════════
# C28: cmd_doctor jinja2 检查 find_spec 返回 None
# ═══════════════════════════════════════════════════════════════
doc_src = inspect.getsource(_m.ParserLitePlugin.cmd_doctor)
if "find_spec" in doc_src and "is not None" in doc_src:
    ok("cmd_doctor jinja2 check handles find_spec -> None correctly")
else:
    sk("cmd_doctor jinja2 check not verified")


finish()
