"""上游 standalone render 兼容补丁 (0 侵入).

1. safe_src 缺 method: 上游模板省略 method 参数
   ({% set src = cont | safe_src %} / safe_src(return_none_on_fail=True))
   → 包装 safe_src 给默认值 "get_path".

2. HTML 源码注入 content: 部分平台 (微博评论等) 把 API 原始 HTML
   字符串放进 content/comments, 模板 {{ | e }} 转义后图片显示标签源码.
   → 包装 render_image, 渲染前就地清洗 HTML 字符串为纯文本.
"""

from __future__ import annotations

import functools
import re
from typing import Any


def strip_html_to_text(text: str) -> str:
    """HTML 源码 → 纯文本 (BeautifulSoup 浏览器级解析).

    解析器已正确解析 HTML 进 content (根因修复); 此处为渲染层防御,
    仅处理残留 HTML 字符串.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _is_html(text: str) -> bool:
    return bool(re.search(r"(?i)</?[a-zA-Z][^>]*>", text))


def _clean_items(items: list[Any]) -> None:
    """就地清洗 content 列表中的 HTML 字符串 (0 侵入, 不重建对象)."""
    for i, item in enumerate(items):
        if isinstance(item, str) and _is_html(item):
            items[i] = strip_html_to_text(item)


def lyric_to_text(lyric) -> str:
    """歌词 dict/list → 文本 (网易云新格式 {t, c:[{tx}]} / 标准 lrc)."""
    if isinstance(lyric, str):
        return lyric
    if isinstance(lyric, list):
        lines = []
        for item in lyric:
            if isinstance(item, dict):
                c = item.get("c")
                if isinstance(c, list):
                    lines.append("".join(x.get("tx", "") for x in c
                                          if isinstance(x, dict) and x.get("tx")))
                elif isinstance(c, str):
                    lines.append(c)
            elif isinstance(item, str):
                lines.append(item)
        return "\n".join(lines)
    if isinstance(lyric, dict):
        if isinstance(lyric.get("lyric"), str):
            return lyric["lyric"]
        if isinstance(lyric.get("c"), list):
            return lyric_to_text(lyric["c"])
    return ""


def clean_result_html(result: Any) -> None:
    """渲染入口防御: 仅处理歌词 dict→文本 (多平台 lyric 字段格式差异).

    注: content/comments 的 HTML 已在解析器层修复 (微博 BS4 / 上游 API 纯文本),
    此处不再重复清洗 (避免补丁堆叠).
    """
    if result is None:
        return
    extra = getattr(result, "extra", None)
    if isinstance(extra, dict) and "lyric" in extra:
        extra["lyric"] = lyric_to_text(extra["lyric"])
    repost = getattr(result, "repost", None)
    if repost is not None:
        clean_result_html(repost)


def pl_esc(value) -> str:
    """转义为普通 str (非 Markup) — 供模板 ~ 拼接使用.

    Jinja2 内置 e filter 返回 Markup, ~ 拼接时会把字面量 HTML 转义
    (str ~ Markup → 转义 str 侧), 导致卡片结构显示为标签文本.
    """
    import html as _h

    return _h.escape(str(value), quote=True)


def pl_str(value) -> str:
    """Markup → 普通 str (内容原样) — 供 join 前 map 使用.

    jinja2 join 在 autoescape 下遇 Markup 元素会转义其他 str 元素
    (Markup.join 语义), 需先统一转普通 str 再 join.
    """
    return str(value)


def apply_render_patch() -> bool:
    """safe_src 默认 method + render_image 入口清洗 HTML.

    :return: 是否已应用
    """
    try:
        import nonebot_plugin_parser_lite.render as _render
        if getattr(_render.safe_src, "_pl_default_method", False):
            return True  # 已 patch

        _orig_safe_src = _render.safe_src

        @functools.wraps(_orig_safe_src)
        async def _patched_safe_src(obj: Any, method: str = "get_path", **kw):
            return await _orig_safe_src(obj, method, **kw)

        _patched_safe_src._pl_default_method = True  # type: ignore[attr-defined]
        _render.safe_src = _patched_safe_src

        # ② render_image 入口清洗 HTML 字符串 (微博评论等平台)
        _orig_render_image = _render.RENDERER.render_image

        @functools.wraps(_orig_render_image)
        async def _patched_render_image(result, *args, **kwargs):
            clean_result_html(result)
            return await _orig_render_image(result, *args, **kwargs)

        _patched_render_image._pl_html_clean = True  # type: ignore[attr-defined]
        _render.RENDERER.render_image = _patched_render_image

        # ③ render_html 重建 env: 注入 pl_esc/pl_str (模板 ~ 拼接与 join 转义修复)
        # 注意: 赋值到实例属性 → 必须是"无 self"裸函数 (调用方直接传 result),
        # 否则 render_image 内部 self.render_html(result) 会把 result 当 self.
        _orig_render_html = _render.RENDERER.render_html
        _renderer = _render.RENDERER

        @functools.wraps(_orig_render_html)
        async def _patched_render_html(result, *args, **kwargs):
            from jinja2 import Environment, FileSystemLoader

            from nonebot_plugin_parser_lite.render import get_theme

            environment = Environment(
                loader=FileSystemLoader(str(_renderer.templates_dir)),
                enable_async=True,
                autoescape=True,
            )
            environment.filters["safe_src"] = _render.safe_src
            environment.filters["pl_esc"] = pl_esc
            environment.filters["pl_str"] = pl_str
            template = environment.get_template(_renderer._template_name(result))
            return await template.render_async(
                result=await _renderer.resolve_parse_result(result),
                theme=kwargs.get("theme") or get_theme(),
            )

        _patched_render_html._pl_env_filters = True  # type: ignore[attr-defined]
        _render.RENDERER.render_html = _patched_render_html
        return True
    except Exception:
        return False
