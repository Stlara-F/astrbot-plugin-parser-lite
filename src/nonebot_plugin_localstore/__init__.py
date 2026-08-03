# Auto-generated standalone stub for nonebot_plugin_localstore
from pathlib import Path as _Path

_base = _Path("data").resolve()
def get_plugin_cache_dir():
    return _base / "cache"
def get_plugin_config_dir():
    return _base / "config"
def get_plugin_data_dir():
    return _base / "data"
