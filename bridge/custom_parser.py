"""自定义解析器: 用户通过 WebUI template_list 配置正则提取规则 (与上游解耦)."""

from __future__ import annotations

import json
import re
from typing import ClassVar

from nonebot_plugin_parser_lite.data import (
    ParseResult,
)


class CustomParser:
    """自定义解析器: 用户通过 WebUI template_list 配置正则提取规则"""

    SCHEMA: ClassVar[list[dict]] = [
        {"key": "_header", "type": "text", "desc": "── 基础配置 ──", "default": ""},
        {"key": "name", "type": "string", "desc": "解析器ID (唯一标识)"},
        {"key": "display", "type": "string", "desc": "显示名"},
        {"key": "url_pattern", "type": "string", "desc": "URL匹配正则"},
        {
            "key": "_extract",
            "type": "text",
            "desc": "── 内容提取 (留空=跳过) ──",
            "default": "",
        },
        {"key": "title_re", "type": "string", "desc": "标题正则", "default": ""},
        {"key": "author_re", "type": "string", "desc": "作者正则", "default": ""},
        {"key": "image_re", "type": "string", "desc": "图片正则", "default": ""},
        {"key": "video_re", "type": "string", "desc": "视频正则", "default": ""},
        {"key": "audio_re", "type": "string", "desc": "音频正则", "default": ""},
        {"key": "text_re", "type": "string", "desc": "正文正则", "default": ""},
        {"key": "cover_re", "type": "string", "desc": "封面正则", "default": ""},
        {"key": "timestamp_re", "type": "string", "desc": "时间戳正则", "default": ""},
        {"key": "_http", "type": "text", "desc": "── HTTP 配置 ──", "default": ""},
        {"key": "headers", "type": "text", "desc": "请求头(JSON)", "default": "{}"},
        {"key": "ajax", "type": "bool", "desc": "API模式(POST)", "default": False},
        {"key": "ajax_url", "type": "string", "desc": "API URL", "default": ""},
        {"key": "timeout", "type": "int", "desc": "超时(秒)", "default": 30},
        {"key": "encoding", "type": "string", "desc": "响应编码", "default": ""},
        {"key": "cookie", "type": "string", "desc": "Cookie", "default": ""},
        {"key": "ua", "type": "string", "desc": "User-Agent", "default": ""},
        {"key": "referer", "type": "string", "desc": "Referer", "default": ""},
        {
            "key": "_extras",
            "type": "text",
            "desc": "── 扩展参数 (键值对自由扩展) ──",
            "default": "{}",
        },
        {
            "key": "extras",
            "type": "text",
            "desc": "扩展参数(JSON)",
            "default": "{}",
            "hint": "任意键值对, 注入到请求配置",
        },
    ]
    """字段声明: key=字段键, type=AstrBot类型, desc=描述, default=默认值 — 注入和 __init__ 共用"""

    # 从 SCHEMA 构建默认值查找表
    _DEFAULTS: ClassVar[dict] = {}
    for _s in SCHEMA:
        if "default" in _s:
            _DEFAULTS[_s["key"]] = _s["default"]

    def __init__(self, entry: dict):
        d = self._DEFAULTS
        self._config = entry
        self._name = (
            str(entry.get("name", d.get("name", ""))).strip()
            or str(entry.get("display", "custom")).strip()
        )
        self._display = str(entry.get("display", self._name))
        self._url_re = re.compile(
            str(entry.get("url_pattern", d.get("url_pattern", "")))
        )
        self._title_re = self._compile_opt(entry, "title_re")
        self._author_re = self._compile_opt(entry, "author_re")
        self._image_re = self._compile_opt(entry, "image_re")
        self._video_re = self._compile_opt(entry, "video_re")
        self._audio_re = self._compile_opt(entry, "audio_re")
        self._text_re = self._compile_opt(entry, "text_re")
        self._cover_re = self._compile_opt(entry, "cover_re")
        self._timestamp_re = self._compile_opt(entry, "timestamp_re")
        self._ajax = bool(entry.get("ajax", d.get("ajax", False)))
        self._ajax_url = str(entry.get("ajax_url", d.get("ajax_url", "")))
        self._timeout = int(entry.get("timeout", d.get("timeout", 30)))
        self._encoding = str(entry.get("encoding", d.get("encoding", "")))
        try:
            hdrs = entry.get("headers", d.get("headers", "{}"))
            self._headers = json.loads(hdrs) if isinstance(hdrs, str) else hdrs
        except Exception:
            self._headers = {}
        cookie = str(entry.get("cookie", d.get("cookie", ""))).strip()
        if cookie:
            self._headers.setdefault("Cookie", cookie)
        ua = str(entry.get("ua", d.get("ua", ""))).strip()
        if ua:
            self._headers.setdefault("User-Agent", ua)
        referer = str(entry.get("referer", d.get("referer", ""))).strip()
        if referer:
            self._headers.setdefault("Referer", referer)
        self._extras = {}
        try:
            raw = entry.get("extras", d.get("extras", "{}"))
            self._extras = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            pass
        if self._extras and isinstance(self._extras, dict):
            for k, v in self._extras.items():
                if k not in self._headers:
                    self._headers[str(k)] = str(v)
        self._closed = False

    @staticmethod
    def _compile_opt(entry: dict, key: str):
        v = str(entry.get(key, "")).strip()
        return re.compile(v) if v else None

    @property
    def platform(self):
        from nonebot_plugin_parser_lite.data import Platform

        return Platform(name=self._name, display_name=self._display)

    def search_url(self, url: str):
        m = self._url_re.search(url)
        if not m:
            return (None, None)
        return (url, m)

    async def parse(self, keyword: str, searched):
        import datetime

        import httpx

        async with httpx.AsyncClient(
            headers=self._headers or None, follow_redirects=True
        ) as client:
            if self._ajax and self._ajax_url:
                resp = await client.post(
                    self._ajax_url, json={"url": keyword}, timeout=self._timeout
                )
            else:
                resp = await client.get(keyword, timeout=self._timeout)
            if self._encoding:
                resp.encoding = self._encoding
            text = resp.text
        result = re.sub(r"<[^>]+>", "", text)

        title = ""
        if self._title_re:
            m = self._title_re.search(result)
            if m:
                title = m.group(1) if m.lastindex else m.group(0)

        author_name = ""
        if self._author_re:
            m = self._author_re.search(result)
            if m:
                author_name = m.group(1) if m.lastindex else m.group(0)

        timestr = ""
        if self._timestamp_re:
            m = self._timestamp_re.search(result)
            if m:
                timestr = m.group(1) if m.lastindex else m.group(0)

        texts = []
        if self._text_re:
            for m in self._text_re.finditer(result):
                t = m.group(1) if m.lastindex else m.group(0)
                if t:
                    texts.append(t)

        from nonebot_plugin_parser_lite.creator import Creator
        from nonebot_plugin_parser_lite.data import (
            Author,
            Platform,
            Stats,
        )

        platform_inst = Platform(name=self._name, display_name=self._display)
        author = Author(name=author_name or "未知")

        stats = Stats()
        ts = None
        if timestr:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    ts = datetime.datetime.strptime(timestr, fmt)
                    break
                except ValueError:
                    pass

        content: list = list(texts)
        if self._image_re:
            for m in self._image_re.finditer(result):
                img_url = m.group(1) if m.lastindex else m.group(0)
                if img_url:
                    try:
                        content.append(Creator.image(img_url))
                    except Exception:
                        pass
        if self._video_re:
            for m in self._video_re.finditer(result):
                vid_url = m.group(1) if m.lastindex else m.group(0)
                if vid_url:
                    try:
                        content.append(Creator.video(vid_url))
                    except Exception:
                        pass
        if self._audio_re:
            for m in self._audio_re.finditer(result):
                aud_url = m.group(1) if m.lastindex else m.group(0)
                if aud_url:
                    try:
                        content.append(Creator.audio(aud_url))
                    except Exception:
                        pass

        return ParseResult(
            platform=platform_inst,
            author=author,
            title=title or keyword,
            content=content,
            stats=stats,
            url=keyword,
            timestamp=ts,
        )

    async def aclose(self):
        self._closed = True


# ── helpers ────────────────────────────────────────────────────────────────────
