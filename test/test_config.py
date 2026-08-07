"""
Config bridge & schema injection tests.
Covers: C5, C6, C15, C22, C25, C36
"""

import inspect
import json

import main as _m
from main import BridgeConfig
from test._base import _ROOT, bad, finish, ok

# ═══════════════════════════════════════════════════════════════
# C6: BridgeConfig._source set via AstrBot kwargs path
# ═══════════════════════════════════════════════════════════════
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=99)
if (
    BridgeConfig._source is not None
    and BridgeConfig._source.get("plite_max_size") == 99
):
    ok("_source set via kwargs (AstrBot path)")
else:
    bad(f"_source = {BridgeConfig._source}")


# ═══════════════════════════════════════════════════════════════
# C5: features 开关双向映射 (selected→True, unselected→False)
# ═══════════════════════════════════════════════════════════════
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download", "Headless"])
cfg = BridgeConfig.get_config()
if cfg and cfg.lazy_download is True and cfg.headless is True:
    ok("features True: Lazy Download=True, Headless=True")
else:
    bad(f"lazy_download={cfg.lazy_download}, headless={cfg.headless}")
BridgeConfig._hash = ""
BridgeConfig._source = None
BridgeConfig.configure(plite_max_size=50, features=["Lazy Download"])
cfg2 = BridgeConfig.get_config()
if cfg2 and cfg2.headless is False:
    ok("features False: Headless=False (unselected, explicitly off)")
else:
    bad(f"Headless={cfg2.headless} (expected False when not in features)")


# ═══════════════════════════════════════════════════════════════
# C22: features 标签 → bool 反向映射 (r9: label 单一来源 i18n)
# ═══════════════════════════════════════════════════════════════
cfg3_src = inspect.getsource(_m.BridgeConfig.configure)
if "label(k) in features_list" in cfg3_src:
    ok("features mapping assigns bool directly")
else:
    bad("features mapping missing label check")


# ═══════════════════════════════════════════════════════════════
# C15: _conf_schema.json 骨架检查
# ═══════════════════════════════════════════════════════════════
_schema_path = _ROOT / "_conf_schema.json"
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
            _injected_marker = _ROOT / ".injected"
            if not _extra:
                ok("_conf_schema.json is skeleton")
            elif _injected_marker.exists():
                ok(
                    f"_conf_schema.json: {len(_extra)} injected keys (confirmed by .injected)"
                )
            else:
                bad(
                    f"_conf_schema.json: {len(_extra)} extra keys but .injected MISSING"
                )
else:
    bad("_conf_schema.json missing")


# ═══════════════════════════════════════════════════════════════
# C36: configure 重建 DOWNLOADER 客户端 (r8: 代理体系已收敛直连)
# ═══════════════════════════════════════════════════════════════
if "apply_downloader_proxy" in cfg3_src:
    ok("configure rebuilds downloader client (direct)")
else:
    bad("configure does NOT rebuild downloader client")


# ═══════════════════════════════════════════════════════════════
# C25: __init__.py 不含 setdefault (不破坏 NoneBot 路径)
# ═══════════════════════════════════════════════════════════════
init_path = _ROOT / "src" / "nonebot_plugin_parser_lite" / "__init__.py"
_i_text2 = init_path.read_text("utf-8")
if "setdefault" not in _i_text2:
    ok("__init__.py: no env var setdefault (NoneBot safe)")
else:
    bad("__init__.py still has setdefault")


finish()
