import os

_IS_STANDALONE = os.environ.get("PARSER_LITE_STANDALONE", "") == "1"
PARSER_LITE_STANDALONE = _IS_STANDALONE
PARSER_LITE_PRE = "plite_"
if _IS_STANDALONE:
    _STANDALONE = True

    def _get_flag(flag: str) -> str:
        return os.environ.get(f"{PARSER_LITE_PRE}{flag}", "")
else:
    from nonebot import get_driver

    _STANDALONE = False

    def _get_flag(flag: str) -> str:
        return getattr(get_driver().config, f"{PARSER_LITE_PRE}{flag}", "")
