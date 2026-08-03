# Auto-generated standalone stub for nonebot.rule
class _NonebotMock:
    def __getattr__(self, name):
        return self
    def __call__(self, *args, **kwargs):
        return self
    def __iter__(self):
        return iter([self])
    def __bool__(self):
        return False
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
get_plugin_config = _m
inherit_supported_adapters = _m
logger = _m
on_command = _m
require = _m
to_me = _m

import logging
logger = logging.getLogger("parser-lite")
