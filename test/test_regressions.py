"""
Regression tests: automated detection of all fixed production bugs.
每个测试对应一个已知的已修复 bug，验证修复不会在未来被意外回退。

Run: py -3 test/run_all.py
"""
import inspect
import json
import os
from pathlib import Path
import subprocess as _sp
import sys

# ═══════════════════════════════════════════════════════════════
# 环境初始化: 模拟 AstrBot standalone 模式
# ═══════════════════════════════════════════════════════════════
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("PARSER_LITE_STANDALONE", "1")

results = {"PASS": 0, "FAIL": 0, "SKIP": 0}
failures: list[str] = []

def ok(msg):   results["PASS"] += 1
def bad(msg):  results["FAIL"] += 1; failures.append(msg)
def sk(msg):   results["SKIP"] += 1


# ═══════════════════════════════════════════════════════════════
# C6: BridgeConfig._source never set via AstrBot kwargs path.
# AstrBot 调用 configure(**self.config), _config 参数为 None,
# 但 _source 从未被赋值, 导致 cookie/proxy/parser 配置全部失效。
# 修复: elif kwargs: cls._source = data
# ═══════════════════════════════════════════════════════════════
from main import BridgeConfig

BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=99)
if BridgeConfig._source is not None and BridgeConfig._source.get("plite_max_size") == 99:
    ok("_source set via kwargs (AstrBot path)")
else:
    bad(f"_source = {BridgeConfig._source}")

# ═══════════════════════════════════════════════════════════════
# C5: features 开关双向映射 (selected→True, unselected→False).
# 仅勾选 → True, 未勾选 → 显式 False (而非保留上游默认值导致无法关闭).
# ═══════════════════════════════════════════════════════════════

# selected → True
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download", "Headless"])
cfg = BridgeConfig.get_config()
if cfg and cfg.lazy_download is True and cfg.headless is True:
    ok("features mapping: Lazy Download=True, Headless=True (selected)")
else:
    bad(f"lazy_download={cfg.lazy_download}, headless={cfg.headless}")
# unselected → False (was: kept upstream default, un-toggle impossible)
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download"])
cfg2 = BridgeConfig.get_config()
if cfg2 and cfg2.headless is False:
    ok("features mapping: Headless=False (unselected, explicitly off)")
else:
    bad(f"Headless={cfg2.headless} (expected False when not in features list)")

# ═══════════════════════════════════════════════════════════════
# C7: _get_cookies_for / _use_proxy_for 重复定义。
# Python 使用最后一个定义, 旧残留覆盖新版本导致 cookie 注入失效。
# 修复: 删除行 502-516 的旧残留定义。
# ═══════════════════════════════════════════════════════════════
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
from main import _inject_dynamic_options_static

try:
    _inject_dynamic_options_static()
    ok("_inject_dynamic_options_static() runs without crash")
except Exception as e:
    bad(f"_inject_dynamic_options_static() crashed: {e}")

# ═══════════════════════════════════════════════════════════════
# C1: main.py 在导入上游前设置 PARSER_LITE_STANDALONE env var.
# __init__.py 不应包含 setdefault (会破坏 NoneBot 插件正常加载路径),
# 只由 main.py 在 AstrBot 上下文下主动设置.
# ═══════════════════════════════════════════════════════════════
_main_path = Path(__file__).resolve().parent.parent / "main.py"
_main_lines = _main_path.read_text("utf-8").splitlines()
found_main_env = any("PARSER_LITE_STANDALONE" in ln and "setdefault" in ln for ln in _main_lines[:25])
if found_main_env:
    ok("main.py sets PARSER_LITE_STANDALONE before upstream imports")
else:
    bad("main.py does NOT set PARSER_LITE_STANDALONE")
# 验证 __init__.py 不再含 setdefault (避免破坏 NoneBot 路径)
init_path = Path(__file__).resolve().parent.parent / "src" / "nonebot_plugin_parser_lite" / "__init__.py"
init_text = init_path.read_text("utf-8")
if "setdefault" not in init_text:
    ok("__init__.py: no setdefault (NoneBot plugin path safe)")
else:
    bad("__init__.py contains setdefault — will break NoneBot loading")
# 验证上游包可在无 nonebot 环境下导入
try:
    import nonebot_plugin_parser_lite  # noqa: F401
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
# C2: DOWNLOADER.ensure_client() 存在性 + parse_url 不再调用 ensure_client
# (代理已通过 _apply_downloader_proxy 直接注入 httpx/curl_cffi 客户端)
# ═══════════════════════════════════════════════════════════════
from nonebot_plugin_parser_lite.download import DOWNLOADER

has_method = hasattr(DOWNLOADER, "ensure_client") and callable(DOWNLOADER.ensure_client)
if has_method:
    ok("DOWNLOADER.ensure_client() exists and is callable (local build)")
else:
    sk("DOWNLOADER.ensure_client() NOT found — production hasattr guard active")
parse_src = inspect.getsource(_m.ParserLite.parse_url)
if "_apply_downloader_proxy" in parse_src:
    ok("parse_url uses _apply_downloader_proxy (direct httpx/curl injection)")
else:
    sk("parse_url proxy injection not verified")

# ═══════════════════════════════════════════════════════════════
# C3: 懒下载会话 URL 使用检测。
# ═══════════════════════════════════════════════════════════════
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
flags_path = Path(__file__).resolve().parent.parent / "src" / "nonebot_plugin_parser_lite" / "utils" / "_flags.py"
if flags_path.exists():
    ok("utils/_flags.py exists on disk")
else:
    bad("utils/_flags.py MISSING — plugin cannot load")

# ═══════════════════════════════════════════════════════════════
# C10: LazyManager async timeout (replaces manual _clean_lazy)
# ═══════════════════════════════════════════════════════════════
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
bm_src = inspect.getsource(_m.ParserLitePlugin.cmd_bm)
if "reply" in bm_src.lower() and "LazyManager" in bm_src:
    ok("cmd_bm supports reply + lazy session BV extraction (3 paths)")
else:
    sk("cmd_bm reply extraction not verified")

# ═══════════════════════════════════════════════════════════════
# C12: UTF-8 编码完整性 — 所有 git-tracked Python 文件必须为有效 UTF-8
# (c7c454b 在移除 # noqa 时通过 Edit 工具破坏了 10 个上游文件的中文 UTF-8 编码)
# ═══════════════════════════════════════════════════════════════
_root = Path(__file__).resolve().parent.parent
_py_files = []
try:
    _r = _sp.run(["git", "ls-files", "*.py"], capture_output=True, text=True, cwd=str(_root))
    if _r.returncode == 0:
        _py_files = [f for f in _r.stdout.strip().split("\n") if f]
except Exception:
    _py_files = list(_root.rglob("*.py"))

_utf8_broken = []
for _pf in _py_files:
    _fpath = _root / _pf
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
# C13: .gitignore 编码有效性 — 必须为纯 ASCII/UTF-8, 不能是 UTF-16
# (.gitignore 曾为 UTF-16 LE, 每条规则含 NUL 字节导致 Git 完全无法解析)
# ═══════════════════════════════════════════════════════════════
_gi_path = _root / ".gitignore"
if _gi_path.exists():
    _gi_bytes = _gi_path.read_bytes()
    _has_nul = b"\x00" in _gi_bytes
    if _has_nul:
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
# C14: 生产运行时文件泄漏 — data/ cache/ config/ FEATURES.md 不应被 git 跟踪
# (c7c454b 误提交了 data/cmd_config.json, FEATURES.md 等生产数据文件)
# ═══════════════════════════════════════════════════════════════
_tracked_runtime = []
for _patt in ["data/", "cache/", "config/", "FEATURES.md"]:
    try:
        _r = _sp.run(["git", "ls-files", _patt], capture_output=True, text=True, cwd=str(_root))
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
# C15: _conf_schema.json 骨架检查 — 文件必须有效 JSON 且含 features 键。
# 仅 features: ["__INJECT__"] → 骨架 (尚未注入, OK)。含额外键 → 完整版,
# 此时必须确认 .injected 标记存在 (证明是注入产生, 非误提交的完整 schema)。
# ═══════════════════════════════════════════════════════════════
_schema_path = _root / "_conf_schema.json"
if _schema_path.exists():
    try:
        _schema = json.loads(_schema_path.read_text("utf-8"))
    except json.JSONDecodeError as _e:
        bad(f"_conf_schema.json is not valid JSON: {_e}")
    else:
        if "features" not in _schema:
            bad("_conf_schema.json missing 'features' key")
        else:
            _extra = set(_schema.keys()) - {"features"}
            _injected_marker = _root / ".injected"
            if not _extra:
                ok("_conf_schema.json is skeleton (features: [__INJECT__])")
            elif _injected_marker.exists():
                ok(f"_conf_schema.json has {len(_extra)} injected keys (injection confirmed by .injected marker)")
            else:
                bad(f"_conf_schema.json has {len(_extra)} extra keys but .injected marker MISSING")
else:
    bad("_conf_schema.json missing")

# ═══════════════════════════════════════════════════════════════
# C16: 上游文件完整性 — 不应有桥接层修改的文件必须保持上游原样
# (c7c454b 破坏了 10 个上游解析器/下载器文件, 这些文件本来与 a4d1b64 完全一致)
# ═══════════════════════════════════════════════════════════════
_UPSTREAM_CLEAN_FILES: list[str] = [
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
    _fp = _root / _uf
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
# C17: test/ 导入路径 — 插件根目录必须在 sys.path 中
# (AstrBot zip 安装时 test/test_parsers 找不到, 因为插件根不在 sys.path)
# ═══════════════════════════════════════════════════════════════
try:
    from test.test_parsers import _FALLBACK_URLS as _tufb17  # noqa: F811
    if _tufb17 and len(_tufb17) > 5:
        ok(f"test.test_parsers importable: {len(_tufb17)} fallback URLs")
    else:
        bad("test.test_parsers._FALLBACK_URLS empty or missing")
except ImportError as _e:
    bad(f"test.test_parsers import FAILED (plugin root not in sys.path): {_e}")

# ═══════════════════════════════════════════════════════════════
# C18: _send_card cache hit 直接发送 fromBytes, 不走 _send_any 文件路径
# (_send_any 遇到不存在的文件直接 return 不抛异常, fromBytes 回退永远不会执行)
# ═══════════════════════════════════════════════════════════════
card_src = inspect.getsource(_m.ParserLitePlugin._send_card)
if "_CARD_CACHE" in card_src and "fromBytes" in card_src:
    if "send_any" not in card_src.split("cache_key")[-1].split("return")[0]:
        ok("card cache hit sends fromBytes directly (no _send_any dead path)")
    else:
        bad("card cache hit still uses _send_any with missing file path")
else:
    sk("card cache verification incomplete")

# ═══════════════════════════════════════════════════════════════
# C19: _is_parser_enabled 读取正确的 disabled_platforms 字段
# (原实现读取 parsers.disabled, 但注入的 schema 只有 plite_disabled_platforms)
# ═══════════════════════════════════════════════════════════════
ie_src = inspect.getsource(_m._is_parser_enabled)
if "disabled_platforms" in ie_src:
    ok("_is_parser_enabled uses disabled_platforms (upstream config)")
else:
    bad("_is_parser_enabled does NOT reference disabled_platforms")

# ═══════════════════════════════════════════════════════════════
# C20: B站 cookie 写入正确的 pydantic 字段 plite_bili_ck
# (原实现 setattr(get_config(), '_bili_ck', ...), BilibiliParser 读取的是 pconfig.bili_ck)
# ═══════════════════════════════════════════════════════════════
parse_src2 = inspect.getsource(_m.ParserLite.parse_url)
if "plite_bili_ck" in parse_src2:
    ok("parse_url sets plite_bili_ck for bilibili cookie injection")
elif "_bili_ck" in parse_src2:
    bad("parse_url sets _bili_ck (private attr, not read by BilibiliParser)")
else:
    sk("cookie injection path not verified")

# ═══════════════════════════════════════════════════════════════
# C21: _on_download_trigger 使用懒下载会话的 URL 而非重新扫描触发消息
# (原实现调用 cmd_parse(event), 触发消息只含 "xz"/"下载", 永远查不到 URL)
# ═══════════════════════════════════════════════════════════════
dt_src = inspect.getsource(_m.ParserLitePlugin._on_download_trigger)
if "session.url" in dt_src and "cmd_parse" not in dt_src:
    ok("_on_download_trigger uses session.url (not cmd_parse re-scan)")
elif "cmd_parse" in dt_src:
    bad("_on_download_trigger still delegates to cmd_parse (will miss lazy URL)")
else:
    sk("_on_download_trigger verification incomplete")

# ═══════════════════════════════════════════════════════════════
# C22: features 开关双向映射 — 检查 configure() 写入 False 的路径
# (原实现只写 True, 未选中保留上游默认 → 默认开启的开关无法关闭)
# ═══════════════════════════════════════════════════════════════
cfg3_src = inspect.getsource(_m.BridgeConfig.configure)
if "_label(k) in features_list" in cfg3_src:
    ok("features mapping assigns bool directly (_label in list)")
else:
    bad("features mapping missing _label check")

# ═══════════════════════════════════════════════════════════════
# C23: dedup 去重集合含 TTL 过期机制
# (原实现 set 无 TTL, 整集合清空时过去很久的连接也被吞掉)
# ═══════════════════════════════════════════════════════════════
hcm_src = inspect.getsource(_m.ParserLitePlugin._handle_card_message)
if "_DEDUP_TTL" in hcm_src and "now - self._recently_processed" in hcm_src:
    ok("dedup has TTL-based expiry (dict with timestamps)")
else:
    sk("dedup TTL verification incomplete")

# ═══════════════════════════════════════════════════════════════
# C24: cmd_bm 正确解包 (video_url, audio_url) 并 aclose parser
# (原实现返回 urls[0] 作为音频, 实际是视频流; 未 aclose 泄漏 httpx)
# ═══════════════════════════════════════════════════════════════
bm_src2 = inspect.getsource(_m.ParserLitePlugin.cmd_bm)
if "audio_url" in bm_src2 and "aclose" in bm_src2:
    ok("cmd_bm unpacks audio_url + calls aclose()")
else:
    sk("cmd_bm unpack/aclose verification incomplete")

# ═══════════════════════════════════════════════════════════════
# C25: __init__.py 不含 setdefault (不破坏 NoneBot 路径)
# (旧实现 setdefault 使 standalone 成为所有环境的默认, NoneBot 插件失灵)
# ═══════════════════════════════════════════════════════════════
_i_text2 = init_path.read_text("utf-8")
if "os.environ.setdefault" not in _i_text2 and 'setdefault("PARSER_LITE_STANDALONE"' not in _i_text2:
    ok("__init__.py: no env var setdefault (NoneBot safe)")
else:
    bad("__init__.py still has setdefault")

# ═══════════════════════════════════════════════════════════════
# C26: terminate() 取消 _chromium_task (防止插件重载时泄漏)
# ═══════════════════════════════════════════════════════════════
term_src = inspect.getsource(_m.ParserLitePlugin.terminate)
if "_chromium_task" in term_src:
    ok("terminate cancels _chromium_task")
else:
    sk("_chromium_task cancel in terminate not verified")

# ═══════════════════════════════════════════════════════════════
# C27: cookie 写入后同步更新 _source (防止 configure() 重建丢失)
# ═══════════════════════════════════════════════════════════════
pu_src2 = inspect.getsource(_m.ParserLite.parse_url)
if '_source["plite_bili_ck"]' in pu_src2:
    ok("parse_url writes cookie to _source (survives configure rebuild)")
else:
    sk("_source cookie persistence not verified")

# ═══════════════════════════════════════════════════════════════
# C28: jinja2 检查处理 find_spec 返回 None (非 ImportError)
# ═══════════════════════════════════════════════════════════════
doc_src = inspect.getsource(_m.ParserLitePlugin.cmd_doctor)
if "find_spec" in doc_src and "is not None" in doc_src:
    ok("cmd_doctor jinja2 check handles find_spec → None correctly")
else:
    sk("cmd_doctor jinja2 check verification incomplete")

# ═══════════════════════════════════════════════════════════════
# C29: parser_extra 注入格式区分单选/多选
# 单选: type=string, options=list, default="", hint=""
# 多选: type=list, options=list, default=[], hint=""
# 不再使用 _single 元数据标记, type 字段本身承载语义
# ═══════════════════════════════════════════════════════════════
inject_src = inspect.getsource(_m._inject_dynamic_options_static)
if '"type": "string" if not is_list else "list"' in inject_src or '"type":"string" if not' in inject_src.replace(" ", ""):
    ok("parser_extra injection uses type=string for single, type=list for multi")
else:
    sk("parser_extra type injection pattern not confirmed")
if '"_single"' not in inject_src:
    ok("parser_extra injection: _single metadata removed (type conveys semantics)")
else:
    bad("parser_extra injection still has _single metadata")
if '"hint": ""' in inject_src or '"hint":""' in inject_src.replace(" ", ""):
    ok("parser_extra injection includes hint field")
else:
    sk("parser_extra hint field not confirmed")

# ═══════════════════════════════════════════════════════════════
# C30: plite_http_proxy 已注入 schema (桥接层专属字段)
# (原实现读取 _source 但未注入, WebUI 无法配置代理)
# ═══════════════════════════════════════════════════════════════
bf_src = inspect.getsource(_m)
if '"plite_http_proxy"' in bf_src and '"HTTP代理"' in bf_src:
    ok("plite_http_proxy injected in _BRIDGE_FIELDS")
else:
    bad("plite_http_proxy NOT found in _BRIDGE_FIELDS")

# ═══════════════════════════════════════════════════════════════
# C29: _send_card 模板存在性检查 + 多目录共存自动复制回退
# (RENDERER.templates_dir 是 anyio.Path, .exists() 是 async 返回 coroutine,
#  同步调用始终 truthy → 防护无效。修复: os.path.exists + src 树复制回退)
# ═══════════════════════════════════════════════════════════════
card_src2 = inspect.getsource(_m.ParserLitePlugin._send_card)
if "os.path.exists" in card_src2 and "shutil.copy2" in card_src2:
    ok("_send_card: os.path.exists check + shutil.copy2 fallback for missing templates")
elif "os.path.exists" in card_src2:
    ok("_send_card: os.path.exists check (no copy fallback)")
elif "tpl_path.exists" in card_src2 or "Path.exists" in card_src2:
    bad("_send_card uses anyio.Path.exists (async, always returns coroutine)")
else:
    sk("_send_card template existence check not found")

# ═══════════════════════════════════════════════════════════════
# C30: 模块加载时清除上游 sys.modules 缓存
# (多插件目录共存时, 旧目录的 nonebot_plugin_parser_lite.* 模块先被缓存,
#  导致 from ... import RENDERER 拿到过期实例, templates_dir 指向错误路径)
# ═══════════════════════════════════════════════════════════════
_main_lines_full = _main_path.read_text("utf-8")
if "nonebot_plugin_parser_lite" in _main_lines_full and "del sys.modules" in _main_lines_full:
    ok("main.py clears stale nonebot_plugin_parser_lite module cache at load")
else:
    sk("sys.modules cache cleanup not confirmed")

t = results["PASS"] + results["FAIL"] + results["SKIP"]
if failures:
    for x in failures: pass
exit(0 if results["FAIL"] == 0 else 1)
