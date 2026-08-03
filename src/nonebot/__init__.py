# Auto-generated standalone stub for nonebot
class _NonebotMock:
    """Recursive mock: attribute → self, call → self, iter → [self], falsy."""
    def __getattr__(self, name):
        return self
    def __call__(self, *args, **kwargs):
        return self
    def __iter__(self):
        return iter([self])
    def __bool__(self):
        return False
    def __len__(self):
        return 0
    def __repr__(self):
        return "nonebot-mock"
    def __contains__(self, item):
        return True

_m = _NonebotMock()
Depends = _m
Event = _m
Matcher = _m
PluginMetadata = _m
Rule = _m
SUPERUSER = _m
T_State = _m
current_bot = _m
current_event = _m
get_driver = _m
inherit_supported_adapters = _m
logger = _m
on_command = _m
require = _m
to_me = _m

def get_plugin_config(config_cls):
    """Standalone: instantiate the config class with defaults (no nonebot driver)."""
    try:
        return config_cls()
    except Exception:
        return _m

import logging
logger = logging.getLogger("parser-lite")

import sys as _sys

def __getattr__(name):
    sub = _NonebotMock()
    setattr(_sys.modules[__name__], name, sub)
    return sub
