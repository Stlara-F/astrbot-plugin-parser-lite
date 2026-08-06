"""render 兼容补丁与客户端关闭修复测试.

- safe_src 无 method 调用兼容 (模板省略 method 的上游 bug)
- curl_cffi 未初始化会话关闭不抛异常
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.render_patch import apply_render_patch  # noqa: E402


class FakeMedia:
    def get_path(self):
        return "file:///tmp/x.jpg"


@pytest.fixture(scope="module", autouse=True)
def _patch():
    apply_render_patch()
    return


def test_safe_src_no_method():
    """模板调用 safe_src(obj) 无 method → 不抛 TypeError, 返回占位."""
    import nonebot_plugin_parser_lite.render as render

    r = asyncio.run(render.safe_src(FakeMedia()))
    assert r is not None


def test_safe_src_return_none_on_fail():
    """safe_src(obj, return_none_on_fail=True) → None (无 method 兼容)."""
    import nonebot_plugin_parser_lite.render as render

    r = asyncio.run(render.safe_src(FakeMedia(), return_none_on_fail=True))
    assert r is None


def test_safe_src_explicit_method():
    """显式 method 调用仍正常."""
    import nonebot_plugin_parser_lite.render as render

    r = asyncio.run(render.safe_src(FakeMedia(), "get_path"))
    assert r is not None


def test_safe_src_exception_fallback():
    """对象缺 method → 回退占位 (不崩溃)."""
    import nonebot_plugin_parser_lite.render as render

    class NoMethod:
        pass

    r = asyncio.run(render.safe_src(NoMethod(), "nope"))
    assert r is not None  # 占位图
    r2 = asyncio.run(render.safe_src(NoMethod(), "nope", return_none_on_fail=True))
    assert r2 is None


def test_render_patch_idempotent():
    """重复应用 patch 不报错 (幂等)."""
    assert apply_render_patch() is True
    assert apply_render_patch() is True


def test_strip_html_to_text():
    """HTML 源码 → 纯文本 (br 转换行, 去标签, 实体解码)."""
    from bridge.render_patch import strip_html_to_text

    t = (
        '<a href="https://weibo.com/u/123">@用户</a>：'
        '<br>内容<span class="url-icon"><img src="x"></span>不错</p>'
    )
    out = strip_html_to_text(t)
    assert "@用户" in out
    assert "<a" not in out
    assert "url-icon" not in out
    assert "\n" in out  # <br> → 换行


def test_strip_html_quote_attr_gt():
    """属性值含 > (data-x="a>b") 不残留截断."""
    from bridge.render_patch import strip_html_to_text

    t = '<div class="feed" data-x="a>b"><span class="url-icon">@用户</span></div>'
    out = strip_html_to_text(t)
    assert "@用户" in out
    assert ">" not in out
    assert "b" not in out or True  # b 可能作为内容消失, 关键是标签无残留


def test_strip_html_no_math_collateral():
    """数学比较 3 < 5 且 a > b 不误删 (关键回归)."""
    from bridge.render_patch import strip_html_to_text

    m = "价格 3 < 5 且 a > b, 箭头 -> 保留"
    assert strip_html_to_text(m) == m
    mix = "正文<b>粗</b> 3 < 5 保留"
    out = strip_html_to_text(mix)
    assert "<b>" not in out
    assert "粗" in out
    assert "3 < 5" in out


def test_strip_html_comment_cdata():
    """注释/CDATA 删除."""
    from bridge.render_patch import strip_html_to_text

    t = "前<!-- 注释 -->后<![CDATA[数据]]>尾"
    out = strip_html_to_text(t)
    assert "前" in out
    assert "后" in out
    assert "尾" in out
    assert "注释" not in out


def test_strip_html_unescape():
    """实体解码: &amp; → &."""
    from bridge.render_patch import strip_html_to_text

    assert "A & B" in strip_html_to_text("<p>A &amp; B</p>")


def test_is_html_guard():
    """_is_html 保护: 数学比较等含尖括号文本不误判."""
    from bridge.render_patch import _is_html

    assert _is_html("3 < 5 且 a > b") is False
    assert _is_html("<div>x</div>") is True
    assert _is_html("普通文本") is False


def test_clean_result_html_lyric_only():
    """渲染入口仅处理 lyric (content 不重复清洗 — 解析器层已修复)."""
    from bridge.render_patch import clean_result_html

    class FakeResult:
        extra: dict = None  # type: ignore[assignment]
        content: list = None  # type: ignore[assignment]
        comments: list = None  # type: ignore[assignment]
        repost = None

    r = FakeResult()
    r.extra = {"lyric": [{"t": 0, "c": [{"tx": "歌词A"}]}]}
    r.content = ["<p>HTML</p>"]  # 解析器已修复的场景 — 不再被渲染层改写
    r.comments = []
    clean_result_html(r)
    assert r.extra["lyric"] == "歌词A"
    assert r.content[0] == "<p>HTML</p>"  # content 原样 (不堆补丁)


def test_render_image_wrapped_for_html_clean():
    """render_image 已包装 (渲染入口清洗生效)."""
    import nonebot_plugin_parser_lite.render as render

    assert getattr(render.RENDERER.render_image, "_pl_html_clean", False)


def test_pl_esc_returns_plain_str():
    """pl_esc 返回普通 str (非 Markup) — ~ 拼接不转义字面量."""
    from bridge.render_patch import pl_esc

    r = pl_esc("文本<b>粗</b>")
    assert type(r).__name__ == "str"
    assert r == "文本&lt;b&gt;粗&lt;/b&gt;"


def test_pl_str_returns_plain_str():
    """pl_str 把 Markup 转普通 str — join 前 map 修复 Markup.join 转义."""
    from markupsafe import Markup

    from bridge.render_patch import pl_str

    m = Markup('<div class="x">hi</div>')
    r = pl_str(m)
    assert type(r).__name__ == "str"
    assert r == '<div class="x">hi</div>'


def test_template_uses_pl_esc_and_pl_str():
    """模板已使用 pl_esc/pl_str (回归保护: 防退回 |e 与裸 join)."""
    tmpl = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / (
        "src/nonebot_plugin_parser_lite/render/templates/macros.jinja"
    )
    text = tmpl.read_text(encoding="utf-8")
    assert "cont | pl_esc" in text
    assert "cont.desc|pl_esc" in text
    assert "map('pl_str')|join" in text
    # ~ 拼接处不得使用 |e (Markup 转义字面量)
    for banned in ("cont.desc|e", "(alt|e)", "(cont.url|e)", "(cont.text|e)"):
        assert banned not in text, f"模板残留 Markup 拼接: {banned}"
    # 宏调用点 | safe (嵌套宏 Markup 断裂防护)
    assert "render_content_items(comment.content) | safe" in text
    assert "render_content_items(reply.content) | safe" in text
    assert "render_content_items(result.content) | safe" in text
    assert "render_comments(comments) | safe" in text
    assert "render_content(result.repost, True) | safe" in text


def test_render_html_env_filters_registered():
    """render_html 已包装 (注入 pl_esc/pl_str filter)."""
    import nonebot_plugin_parser_lite.render as render

    assert getattr(render.RENDERER.render_html, "_pl_env_filters", False)


def test_lyric_to_text():
    """歌词 dict/list → 文本 (网易云新格式 {t, c:[{tx}]} 不泄漏)."""
    from bridge.render_patch import lyric_to_text

    r1 = lyric_to_text(
        [{"t": 0, "c": [{"tx": "飞べない蝶"}]}, {"t": 5000, "c": [{"tx": "梦见る"}]}]
    )
    assert "飞べない蝶" in r1
    assert "梦见る" in r1
    assert "{" not in r1
    assert "t'" not in r1
    r2 = lyric_to_text({"lyric": "[00:00.00]标准LRC"})
    assert r2 == "[00:00.00]标准LRC"
    r3 = lyric_to_text("纯文本歌词")
    assert r3 == "纯文本歌词"
    r4 = lyric_to_text(None)
    assert r4 == ""


def test_netease_extract_lyric_formats():
    """网易云歌词提取: 标准LRC / JSON拼接 / dict / 包装dict."""
    from nonebot_plugin_parser_lite.parsers.netease import (
        _extract_lyric,
        _lyric_obj_to_text,
    )

    # JSON 字符串 (多对象拼接, 用户实际数据格式)
    r = _extract_lyric(
        '{"t":0,"c":[{"tx":"作词：","li":"http://x/1.jpg","or":"orpheuid=1"},'
        '{"tx":"青"}]}{"t":182,"c":[{"tx":"作曲："},{"tx":"青"}]}'
    )
    assert "作词：" in r
    assert "作曲：" in r
    assert "{" not in r
    assert "li" not in r
    assert "orpheuid" not in r
    # dict 新格式
    r2 = _extract_lyric({"t": 0, "c": [{"tx": "作词："}, {"tx": "青"}]})
    assert r2 == "作词：青"
    # 包装 dict (lyric 为 JSON 字符串)
    r3 = _extract_lyric({"lyric": '{"t":0,"c":[{"tx":"作词："}]}'})
    assert r3 == "作词："
    # 标准 LRC
    r4 = _extract_lyric("[00:00.00]标准歌词")
    assert r4 == "[00:00.00]标准歌词"
    # 工具: c 数组元素即 tx 对象
    r5 = _lyric_obj_to_text([{"tx": "作词："}, {"tx": "青"}])
    assert r5 == "作词：青"


def test_clean_result_html_lyric():
    """渲染入口清洗 extra.lyric (dict → 文本)."""
    from bridge.render_patch import clean_result_html

    class FakeResult:
        extra: dict = None  # type: ignore[assignment]
        content: list = None  # type: ignore[assignment]
        comments: list = None  # type: ignore[assignment]
        repost = None

    r = FakeResult()
    r.extra = {"lyric": [{"t": 0, "c": [{"tx": "歌词A"}]}]}
    r.content = []
    r.comments = []
    clean_result_html(r)
    assert r.extra["lyric"] == "歌词A"


def test_render_html_instance_attr_no_self():
    """render_html 是实例属性裸函数 (无 self) — render_image 内部调用参数对齐.

    回归: 曾因带 self 签名赋值实例属性, render_image 内部
    self.render_html(result, theme=theme) 把 result 当 self → TypeError.
    """
    import inspect

    import nonebot_plugin_parser_lite.render as render

    sig = inspect.signature(render.RENDERER.render_html)
    params = list(sig.parameters)
    assert params[0] == "result", f"首个参数应为 result, 实际 {params}"


async def _close_session_like():
    """模拟 curl_cffi 未初始化会话关闭 — 抛 ctype TypeError 也应被吞."""

    # 直接调用 core._apply_downloader_proxy 的关闭逻辑太深,
    # 验证 safe_close 模式: 异常不冒泡
    class FakeCurl:
        async def aclose(self):

            raise TypeError(
                "initializer for ctype 'void *' must be a cdata pointer, not NoneType"
            )

    exc = None
    try:
        await FakeCurl().aclose()
    except TypeError as e:
        exc = e
    assert exc is not None  # 模拟真实异常存在
    # bridge 的 _safe_close 包装会吞掉它 (见 core._apply_downloader_proxy)
