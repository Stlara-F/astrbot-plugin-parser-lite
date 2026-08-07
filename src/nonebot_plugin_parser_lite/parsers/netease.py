import contextlib
import json
import random
import time
from typing import Any, ClassVar

from .base import (
    BaseParser,
    ContentItem,
    MatchWithParams,
    ParseException,
    Platform,
    PlatformEnum,
    handle,
)


def _lyric_obj_to_text(obj: Any) -> str:
    """网易云新格式歌词对象 → 文本.

    {"t": 0, "c": [{"tx": "作词：", "li": "...", "or": "..."}, {"tx": "青"}]}
    → "作词：青"  (li/or 为图片/链接信息, 忽略)
    """
    if isinstance(obj, list):
        # c 数组元素即 tx 对象: [{"tx": "作词："}, {"tx": "青"}]
        return "".join(x.get("tx", "") for x in obj if isinstance(x, dict))
    if isinstance(obj, dict) and isinstance(obj.get("c"), list):
        return _lyric_obj_to_text(obj["c"])
    return ""


def _parse_lyric_json(s: str) -> str | None:
    """解析歌词 JSON (支持多个对象拼接: {...}{...}{...}). 失败返回 None."""
    decoder = json.JSONDecoder()
    idx = 0
    n = len(s)
    texts: list[str] = []
    while idx < n:
        while idx < n and s[idx] in " \n\r\t":
            idx += 1
        if idx >= n:
            break
        if s[idx] != "{":
            return None
        try:
            obj, end = decoder.raw_decode(s, idx)
        except json.JSONDecodeError:
            return None
        texts.append(_lyric_obj_to_text(obj))
        idx = end
    return "\n".join(texts) if texts else None


def _extract_lyric(lrc_data: Any) -> str:
    """从 getSongLyric 的 lrc 字段提取歌词文本.

    支持:
    - 标准 LRC 字符串: "[00:00.00]作词：青"
    - 新版 JSON 字符串 (可多对象拼接): '{"t":0,"c":[{"tx":"作词："}]}{"t":182,...}'
    - 新版 dict: {"t":0,"c":[{"tx":"作词："}]}
    - 标准包装 dict: {"lyric": "<上述任意一种>"}
    """
    if isinstance(lrc_data, str):
        s = lrc_data.strip()
        if s.startswith("{"):
            parsed = _parse_lyric_json(s)
            if parsed is not None:
                return parsed
        return lrc_data
    if isinstance(lrc_data, dict):
        inner = lrc_data.get("lyric")
        if isinstance(inner, str):
            s = inner.strip()
            if s.startswith("{"):
                parsed = _parse_lyric_json(s)
                if parsed is not None:
                    return parsed
            return inner
        if isinstance(lrc_data.get("c"), list):
            return _lyric_obj_to_text(lrc_data)
    return ""


def random_ip() -> str:
    return ".".join(str(random.randint(0, 255)) for _ in range(4))


def parse_duration_to_seconds(duration: str) -> int:
    """将时长字符串解析为总秒数。"""
    parts = duration.split(":")
    if not (1 <= len(parts) <= 3):
        raise ValueError(f"非法的时长格式: {duration!r}")

    try:
        parts_int = [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"时长中包含非法数字: {duration!r}") from exc

    if len(parts_int) == 1:
        hours = 0
        minutes = 0
        seconds = parts_int[0]
    elif len(parts_int) == 2:
        hours = 0
        minutes, seconds = parts_int
    else:
        hours, minutes, seconds = parts_int

    if not (0 <= seconds < 60 and 0 <= minutes < 60 and hours >= 0):
        raise ValueError(f"时长数值不合法: {duration!r}")

    return hours * 3600 + minutes * 60 + seconds


class NCMParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name=PlatformEnum.NETEASE, display_name="网易云音乐"
    )

    def __init__(self):
        super().__init__()
        self.httpx.headers.update({"Referer": "https://wyapi.toubiec.cn/"})
        self.httpx.base_url = "https://nextmusic.toubiec.cn/api"

    async def fetch(self, endpoint: str, payload: dict) -> dict:
        payload["timestamp"] = int(time.time() * 1000)
        payload["ip"] = random_ip()
        resp = await self.httpx.post(endpoint, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 200:
            raise ParseException(f"接口返回错误: {result}")
        return result["data"]

    @handle("163cn.tv", r"https?://[^\s]*?163cn\.tv/[a-zA-Z0-9]+")
    async def _parse_163cn(self, searched: MatchWithParams):
        return await self.parse_with_redirect(searched[0])

    @handle("y.music.163.com", params={"id": {"as_int": True}})
    @handle("music.163.com", params={"id": {"as_int": True}})
    @handle("music.163.com", r"song/(?P<id>\d+)")
    async def _parse_netease(self, searched: MatchWithParams):
        ncm_id = searched["id"]
        song = await self.fetch("getSongInfo", {"id": ncm_id})
        title = song.get("name", "未知")
        artist = song.get("singer", "未知歌手")
        duration = parse_duration_to_seconds(song.get("duration", "0"))
        lyric = ""
        with contextlib.suppress(Exception):
            lrc_data = (await self.fetch("getSongLyric", {"id": ncm_id})).get("lrc")
            lyric = _extract_lyric(lrc_data)
        url_data = await self.fetch("getSongUrl", {"id": ncm_id, "level": "standard"})
        if not (audio_url := url_data.get("url")):
            raise ParseException("无法获取音频下载地址")
        url_no_params = audio_url.split("?", 1)[0]
        ext = url_no_params.rsplit(".", 1)[-1].lower() if "." in url_no_params else ""
        audio_type = ext if ext in {"flac", "wav", "m4a", "aac", "mp3"} else "mp3"
        contents: list[ContentItem] = []

        audio_name = f"{title}-{artist}.{audio_type}"
        audio = self.create_audio(
            audio_url,
            duration=duration,
            audio_name=audio_name,
        )
        contents.append(audio)

        if cover_url := song.get("picimg"):
            contents.append(self.create_image(cover_url))

        audio_info = f"大小: {await audio.get_display_size()} | 格式: {audio_type}"

        extra = {
            "info": audio_info,
            "lyric": lyric,
        }

        return self.result(
            title=title,
            author=self.create_author(name=artist),
            url=f"https://music.163.com/song/{ncm_id}",
            content=contents,
            extra=extra,
        )
