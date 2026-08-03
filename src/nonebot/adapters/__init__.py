# Auto-generated standalone stub for nonebot.adapters
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
ADMIN = _m
Alconna = _m
Args = _m
CustomNode = _m
Depends = _m
Event = _m
Extension = _m
File = _m
Hyper = _m
Image = _m
Match = _m
Matcher = _m
Reference = _m
Rule = _m
SUPERUSER = _m
Segment = _m
SupportAdapter = _m
T_State = _m
Text = _m
UniMessage = _m
UniMsg = _m
Uninfo = _m
Video = _m
Voice = _m
cache_msg = _m
current_bot = _m
current_event = _m
get_driver = _m
get_message_id = _m
get_plugin_config = _m
logger = _m
on_alconna = _m
on_command = _m
reply_fetch = _m
template_to_pic = _m
to_me = _m
uniseg = _m

import logging
logger = logging.getLogger("parser-lite")
