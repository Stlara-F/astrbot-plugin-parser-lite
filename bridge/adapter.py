"""事件适配 (bridge-core 超薄桥接: context + url_extract + platform 合并).

- 上游引用聚合: up_config/up_downloader/up_renderer/up_base_parser/up_creator
  (延迟 import, CI/离线无上游可导入)
- URL 提取: extract_urls/extract_reply_urls (AstrBot 消息段 → URL 列表)
- 平台配置: enabled_platforms/cookies 同步 (bridge 勾选 → 上游 disabled 过滤)
- _is_parser_enabled: 平台启用判定 (与 enabled 列表收敛)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

# ── 上游引用 (延迟 import: CI/离线测试无上游时保持可导入) ──────────────────

_UP_CONFIG: Any = None
_UP_DOWNLOADER: Any = None
_UP_RENDERER: Any = None
_UP_BASE_PARSER: Any = None
_UP_CREATOR: Any = None


def _import_upstream() -> None:
    """按需加载上游模块 (standalone 模式)."""
    global _UP_CONFIG, _UP_DOWNLOADER, _UP_RENDERER, _UP_BASE_PARSER, _UP_CREATOR
    if _UP_CONFIG is None:
        from nonebot_plugin_parser_lite.config import Config as _UP_CONFIG
    if _UP_DOWNLOADER is None:
        from nonebot_plugin_parser_lite.download import DOWNLOADER as _UP_DOWNLOADER
    if _UP_RENDERER is None:
        from nonebot_plugin_parser_lite.render import RENDERER as _UP_RENDERER
    if _UP_BASE_PARSER is None:
        from nonebot_plugin_parser_lite.parsers.base import (
            BaseParser as _UP_BASE_PARSER,
        )
    if _UP_CREATOR is None:
        from nonebot_plugin_parser_lite.creator import Creator as _UP_CREATOR


def __up_config():
    _import_upstream()
    return _UP_CONFIG


def up_downloader():
    _import_upstream()
    return _UP_DOWNLOADER


def up_renderer():
    _import_upstream()
    # 自动确保渲染补丁 (safe_src 默认 method + pl_esc/pl_str 注册, 幂等)
    # 上游模板省略 method 且引用 pl_esc/pl_str — 任何渲染调用方都需要
    try:
        from bridge.render import apply_render_patch

        apply_render_patch()
        # 引用对齐: main.py 清 sys.modules 会重建上游模块 → 缓存必须跟随
        # (否则 patch 打到新模块实例, 调用仍走旧实例 → pl_esc 未注册)
        import nonebot_plugin_parser_lite.render as _render_mod

        global _UP_RENDERER
        _UP_RENDERER = _render_mod.RENDERER
    except Exception:
        pass
    return _UP_RENDERER


def up_base_parser():
    _import_upstream()
    # 惰性发现: 显式注册全部平台解析器
    from nonebot_plugin_parser_lite.parsers import load_all as _load_all

    _load_all()
    return _UP_BASE_PARSER


def up_creator():
    _import_upstream()
    return _UP_CREATOR


URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)


def url_from_text(get_message_str) -> str | None:
    """从消息文本提取首个 URL."""
    m = URL_RE.search(str(get_message_str()).strip())
    return m.group(0) if m else None


def collect_urls(text: str, urls: list[str]) -> None:
    for m in URL_RE.finditer(text):
        urls.append(m.group(0))


def extract_card_json_url(data) -> str | None:
    """从 Json 组件 .data 中动态提取 URL (0 hardcode: 递归扫描所有含 url/link 键的值)"""
    try:
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    # BFS 递归扫描: 优先匹配命名键 (含 url/link 字样), 兜底全文字符串
    queue: list[tuple] = [(data, "")]
    named_urls: list[str] = []
    raw_urls: list[str] = []

    while queue:
        obj, pkey = queue.pop(0)
        if isinstance(obj, dict):
            for k, v in obj.items():
                queue.append((v, str(k).lower()))
        elif isinstance(obj, list):
            for item in obj:
                queue.append((item, pkey))
        elif isinstance(obj, str) and len(obj) > 10:
            if "url" in pkey or "link" in pkey:
                if obj.startswith("http"):
                    named_urls.append(obj)
            elif URL_RE.match(obj):
                if not any(
                    x in obj.lower()
                    for x in (
                        "icon",
                        "logo",
                        "avatar",
                        "thumbnail",
                        "imageview",
                        ".png",
                        ".jpg",
                        ".ico",
                    )
                ):
                    raw_urls.append(obj)

    return named_urls[0] if named_urls else raw_urls[0] if raw_urls else None


def extract_json_urls(raw: str | dict, urls: list[str]) -> None:
    """从 JSON 卡片 data 中提取嵌入 URL (递归 BFS)"""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    queue = [payload]
    keys = (
        "url",
        "jumpUrl",
        "qqdocurl",
        "share_url",
        "jump_url",
        "link",
        "action_url",
        "source_url",
        "redirect_url",
        "preview_url",
        "article_url",
    )
    seen_objs = set()
    while queue:
        obj = queue.pop(0)
        if id(obj) in seen_objs:
            continue
        seen_objs.add(id(obj))
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k, "")
                if isinstance(v, str) and URL_RE.match(v):
                    urls.append(v)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(obj, list):
            queue.extend(obj)


def extract_xml_urls(raw: str, urls: list[str]) -> None:
    """从 XML 卡片中提取 URL"""
    for tag in ("url", "qqdocurl", "jumpUrl", "share_url", "link"):
        for m in re.finditer(
            rf"""<{tag}>\s*(https?://[^<\s]+)\s*</{tag}>""", raw, re.IGNORECASE
        ):
            urls.append(m.group(1).strip())
    for attr in ("url", "qqdocurl", "jumpUrl", "share_url"):
        for m in re.finditer(
            rf"""{attr}\s*=\s*['"](https?://[^\s'"<>]+)['"]""", raw, re.IGNORECASE
        ):
            urls.append(m.group(1).strip())


def extract_forward_urls(seg_data, urls: list[str]) -> None:
    """从合并转发节点中递归提取 URL"""
    if isinstance(seg_data, dict):
        msgs = seg_data.get("messages", []) or seg_data.get("content", [])
    elif isinstance(seg_data, str):
        try:
            parsed = json.loads(seg_data)
            msgs = parsed.get("messages", []) if isinstance(parsed, dict) else []
        except (json.JSONDecodeError, TypeError):
            return
    else:
        msgs = seg_data if isinstance(seg_data, list) else []
    if not isinstance(msgs, list):
        return
    for node in msgs:
        if not isinstance(node, dict):
            continue
        sub_msgs = node.get("message", None) or node.get("content", None)
        if isinstance(sub_msgs, list):
            for sub in sub_msgs:
                if isinstance(sub, dict):
                    sub_data = sub.get("data", {})
                    if isinstance(sub_data, dict):
                        for f in ("text", "content", "url"):
                            v = sub_data.get(f, "")
                            if v:
                                collect_urls(str(v), urls)
            for sub in sub_msgs:
                if isinstance(sub, dict) and sub.get("type") in ("forward", "node"):
                    extract_forward_urls(sub.get("data", {}), urls)
        elif isinstance(sub_msgs, str):
            collect_urls(sub_msgs, urls)


def extract_urls(event, comp_cls) -> list[str]:
    """从 AstrBot 事件中提取全部 URL (Comp 类以参数传入).

    :param event: AstrBot AstrMessageEvent
    :param comp_cls: astrbot.api.message_components 模块 (isinstance 判断)
    """
    urls: list[str] = []

    # 1. 遍历 AstrBot 消息链
    try:
        chain = event.get_messages()
    except Exception:
        chain = []
    if not chain:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            chain = (
                getattr(msg_obj, "message", None)
                or getattr(msg_obj, "message_chain", None)
                or []
            )
    if not isinstance(chain, list):
        chain = []

    for seg in chain:
        if isinstance(seg, comp_cls.Json):
            url = extract_card_json_url(seg.data)
            if url:
                urls.append(url)
        elif isinstance(seg, comp_cls.Image):
            pass  # 图片不含 URL
        else:
            seg_data = None
            seg_type = ""
            if isinstance(seg, dict):
                seg_type = str(seg.get("type", "")).lower()
                seg_data = seg.get("data", {}) or {}
            elif hasattr(seg, "type"):
                seg_type = str(getattr(seg, "type", "")).lower()
                seg_data = getattr(seg, "data", None)

            if seg_type == "text" or "text" in seg_type:
                t = (
                    seg_data.get("text", "")
                    if isinstance(seg_data, dict)
                    else str(seg_data or "")
                )
                collect_urls(t, urls)
            elif "json" in seg_type or "miniapp" in seg_type:
                d = (
                    seg_data.get("data", "")
                    if isinstance(seg_data, dict)
                    else str(seg_data or "")
                )
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                if isinstance(d, str) and d:
                    url = extract_card_json_url(d)
                    if url:
                        urls.append(url)
                    collect_urls(d, urls)
            elif "xml" in seg_type:
                d = (
                    seg_data.get("data", "")
                    if isinstance(seg_data, dict)
                    else str(seg_data or "")
                )
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                extract_xml_urls(d, urls)
            elif "reply" in seg_type:
                if isinstance(seg_data, dict):
                    rt = seg_data.get("text", "") or seg_data.get("message", "") or ""
                    if isinstance(rt, list):
                        rt = " ".join(
                            (
                                s.get("data", {}).get("text", "")
                                if isinstance(s, dict)
                                else str(s)
                            )
                            for s in rt
                        )
                    collect_urls(str(rt), urls)
            elif "markdown" in seg_type:
                d = seg_data.get("data", "") or seg_data.get("content", "")
                if isinstance(d, dict):
                    d = json.dumps(d, ensure_ascii=False)
                collect_urls(str(d or ""), urls)
            elif "forward" in seg_type:
                extract_forward_urls(seg_data or {}, urls)

    # 2. 纯文本兜底
    text = event.get_message_str()
    if text:
        collect_urls(text, urls)

    # 3. 去重 + 清理尾部标点
    seen = set()
    result = []
    for u in urls:
        u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def extract_reply_urls(event) -> list[str]:
    """从被回复消息中提取 URL — 小程序卡片链接的逃生通道 (r11: main.py 下沉).

    兼容 OneBot reply 段 (data.text / data.message) 与 AstrBot message_obj 链.
    """
    urls: list[str] = []
    msg_obj = getattr(event, "message_obj", None)
    chain = getattr(msg_obj, "message", None) or []
    for seg in chain if isinstance(chain, list) else []:
        seg_type = str(seg.get("type", "")) if isinstance(seg, dict) else ""
        if "reply" not in seg_type:
            continue
        data = seg.get("data", {}) if isinstance(seg, dict) else {}
        if not isinstance(data, dict):
            continue
        for key in ("text", "message", "content"):
            raw = data.get(key, "")
            if isinstance(raw, list):
                for sub in raw:
                    if isinstance(sub, dict):
                        sub_data = sub.get("data", {})
                        if isinstance(sub_data, dict):
                            collect_urls(str(sub_data.get("text", "")), urls)
                            d = sub_data.get("data", "")
                            if isinstance(d, str) and d:
                                collect_urls(d, urls)
                                u = extract_card_json_url(d)
                                if u:
                                    urls.append(u)
            elif isinstance(raw, str) and raw:
                collect_urls(raw, urls)
                u = extract_card_json_url(raw)
                if u:
                    urls.append(u)
    # 去重
    seen = set()
    result = []
    for u in urls:
        u = u.strip().rstrip(".,;!?，。；！？〉》）〕")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


_logger = logging.getLogger("nonebot_plugin_parser_lite")


def _log_cfg_fallback(exc: Exception) -> None:
    """统一"配置读取回退"日志 (debug 级, 不刷屏)."""
    _logger.debug(f"[ParserLite] 配置读取回退: {exc}")


_PROXY_LOCK = threading.Lock()


def client_closed(client) -> bool:
    """检测 DOWNLOADER 客户端是否已关闭 (插件重载/terminate 后残留)."""
    for attr in ("_httpx", "_curl"):
        s = getattr(client, attr, None)
        if s is None:
            return True
        if getattr(s, "is_closed", False):
            return True
        if getattr(s, "_curl", None) is None and hasattr(s, "_curl"):
            return True
    return False


async def _safe_close(_c):
    try:
        await _c.aclose() if hasattr(_c, "aclose") else _c.close()
    except (TypeError, RuntimeError, asyncio.CancelledError):
        pass  # curl_cffi 未初始化会话关闭会抛 TypeError — 忽略 (B18: 收窄捕获)


def apply_downloader_proxy(proxy_url: str = ""):
    """重建 DOWNLOADER 客户端 (直连, T2: 代理配置已移除).

    并发安全: threading.Lock 互斥重建 (避免 client 抖动/重复替换).
    早期返回: 客户端存活 (None 视为未初始化 → 需重建).
    :param proxy_url: 兼容旧签名, 恒忽略 (代理体系已收敛直连)
    """
    with _PROXY_LOCK:
        client = up_downloader().client
        _httpx = getattr(client, "_httpx", None)
        _curl = getattr(client, "_curl", None)
        _httpx_alive = _httpx is not None and not getattr(_httpx, "is_closed", False)
        _curl_alive = _curl is not None and not getattr(_curl, "is_closed", False)
        if _httpx_alive and _curl_alive:
            return
        from curl_cffi import AsyncSession as CurlSession
        from httpx import AsyncClient as HttpxClient
        from httpx import Timeout

        # 调度旧客户端关闭 (插件重载后残留客户端需释放)
        for _old_attr in ("_httpx", "_curl"):
            _old = getattr(client, _old_attr, None)
            if _old is not None:
                try:
                    asyncio.get_running_loop().create_task(_safe_close(_old))
                except RuntimeError:
                    # B3: 无运行中 loop → 同步 close 兜底 (释放连接池, 不静默丢弃)
                    try:
                        _old.close()
                    except (TypeError, RuntimeError):
                        pass
        client._httpx = HttpxClient(
            verify=False, follow_redirects=True, timeout=Timeout(timeout=15)
        )
        client._curl = CurlSession(
            impersonate="chrome146", timeout=240, verify=False, allow_redirects=True
        )

    _logger.info("[ParserLite] downloader client rebuilt (direct)")


# ── 平台级决策 (统一勾选列表: platforms.items.{enabled,cookies}) ──

# 平台块惰性缓存: {源哈希: 归一化块}; configure() 更新 _source 后哈希变化自动失效
_PLATFORMS_CACHE: dict[str, dict] = {}


def _platforms_block() -> dict:
    """读取 platforms 配置块 (新格式 object / 旧格式 template_list 迁移).

    AstrBot 生成配置时会把 object 类型配置展平为顶层键:
    schema 形式 {"items": {"enabled": [...]}} → 配置形式 {"enabled": [...]}
    惰性缓存: 配置源哈希不变则复用解析结果 (热路径免重复归一化).
    """
    import hashlib

    from bridge.config import global_source

    _src = global_source()
    # R8: 仅 platforms 段参与哈希 (非 platforms 键变更不误失效缓存)
    _h = hashlib.md5(repr(_src.get("platforms")).encode()).hexdigest()
    _cached = _PLATFORMS_CACHE.get(_h)
    if _cached is not None:
        return _cached
    try:
        pfm = _src.get("platforms", {}) or {}
        if isinstance(pfm, dict):
            if "enabled" in pfm or "cookies" in pfm:
                _block = {"items": pfm}  # AstrBot 展平形态 → 归一化
            else:
                _block = pfm
        elif isinstance(pfm, list):  # 旧 27 模板格式 → 模拟块
            _block = {"items": {}, "_legacy_list": pfm}
        else:
            _block = {}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
        _block = {}
    _PLATFORMS_CACHE[_h] = _block
    return _block


def platform_cfg(platform: str) -> dict:
    """平台配置 (旧 27 模板迁移兼容: enable/cookies; T2: proxy 键已剥离).

    新格式 (items.enabled/cookies) 由 enabled_platforms/cookies_entries 统一读取.
    """
    try:
        _blk = _platforms_block()
        _items = _blk.get("items", {}) if isinstance(_blk, dict) else {}
        # 新格式: 勾选列表内联读取
        if _items:
            _ret = {}
            _enabled = _items.get("enabled", [])
            if isinstance(_enabled, list) and _enabled:
                _ret["enable"] = platform.lower() in _value_set(_enabled)
            # T2: proxied (规则代理) 已移除
            for _ck in _items.get("cookies", []) or []:
                if (
                    isinstance(_ck, dict)
                    and str(_ck.get("platform", "")).lower() == platform.lower()
                ):
                    _ret["cookies"] = str(_ck.get("cookie", "") or "")
                    break
            return _ret
        # 旧格式: template_list / dict (proxy/use_proxy 键剥离)
        _legacy = _blk.get("_legacy_list")
        if isinstance(_legacy, list):
            for item in _legacy:
                if (
                    isinstance(item, dict)
                    and str(item.get("platform", "")).lower() == platform.lower()
                ):
                    return {
                        k: v for k, v in item.items() if k not in ("proxy", "use_proxy")
                    }
        elif isinstance(_blk, dict):
            _legacy_dict = _blk.get(platform, {}) or {}
            if isinstance(_legacy_dict, dict):
                return {
                    k: v
                    for k, v in _legacy_dict.items()
                    if k not in ("proxy", "use_proxy")
                }
            return {}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def _value_set(values: list) -> set[str]:
    """勾选列表值归一化: 支持 {"value": str} / 纯字符串."""
    out = set()
    for v in values or []:
        if isinstance(v, dict):
            v = v.get("value", v.get("label", ""))
        if isinstance(v, str):
            out.add(v.strip().lower())
    return out


def enabled_platforms() -> set[str] | None:
    """新格式勾选启用的平台集合 (None = 未配置 → 全部启用)."""
    try:
        _items = _platforms_block().get("items", {})
        _en = _items.get("enabled", [])
        return _value_set(_en) if isinstance(_en, list) and _en else None
    except Exception:
        return None


def cookies_entries() -> list[dict]:
    """新格式 cookie 条目列表 [{platform, cookie}]."""
    try:
        _items = _platforms_block().get("items", {})
        _ck = _items.get("cookies", [])
        return [
            e
            for e in (_ck or [])
            if isinstance(e, dict) and e.get("platform") and e.get("cookie")
        ]
    except Exception:
        return []


def sync_cookies_to_upstream() -> None:
    """将新格式 cookies 同步至上游 Config 的 plite_<platform>_ck 字段 (动态源).

    仅同步源码声明了 _ck 字段的平台; 无 astrbot/无上游时静默跳过.
    """
    try:
        from bridge.config import up_config as _up_config

        _cfg = _up_config()
        _sync = False
        for entry in cookies_entries():
            _fname = f"plite_{str(entry['platform']).lower()}_ck"
            if (
                _fname in _cfg.model_fields
                and getattr(_cfg, _fname, None) != entry["cookie"]
            ):
                setattr(_cfg, _fname, entry["cookie"])
                _sync = True
        if _sync:
            _logger.info("[ParserLite] cookies synced to upstream config")
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)


def load_parsers_config() -> dict:
    """读取 parsers 配置 (仅旧配置迁移期兼容, 新配置统一 platforms)."""
    try:
        from bridge.config import global_source

        p = (global_source()).get("parsers", {}) or {}
        if isinstance(p, dict):
            inner = p.get("items", p) if isinstance(p.get("items"), dict) else p
            if isinstance(inner, dict):
                return inner
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def get_cookies_for(platform: str) -> dict:
    """平台 Cookie: platforms.items.cookies 条目 (新格式, 自动同步上游).

    旧格式兼容: 27 模板 platforms[].cookies / parsers.items.cookies (废弃).
    """
    import json as _json

    try:
        for entry in cookies_entries():
            if str(entry.get("platform", "")).lower() == platform.lower():
                ck = str(entry.get("cookie", "") or "").strip()
                if ck:
                    return {"Cookie": ck}
        _pc = platform_cfg(platform)
        _ck = (_pc.get("cookies", "") or "").strip()
        if _ck:
            return {"Cookie": _ck}
        # 迁移期兼容: parsers.items.cookies (废弃)
        raw = load_parsers_config().get("cookies", "{}")
        if isinstance(raw, list):
            for entry in raw:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("platform", "")).lower() == platform.lower()
                ):
                    ck = str(entry.get("cookie", "") or "").strip()
                    if ck:
                        return {"Cookie": ck}
            return {}
        cookies = (
            _json.loads(raw)
            if isinstance(raw, str)
            else (raw if isinstance(raw, dict) else {})
        )
        ck = cookies.get(platform, "").strip()
        if ck:
            return {"Cookie": ck}
    except Exception as _cfg_e:
        _log_cfg_fallback(_cfg_e)
    return {}


def build_feature_table() -> dict:
    """动态特征表: URL关键词 → 解析器名 (O(1) 路由, 0 硬编码)."""
    import inspect as _inspect
    import re as _re

    from bridge.adapter import up_base_parser

    table: dict[str, str] = {}
    for cls in up_base_parser().get_all_subclass():
        name = cls.__name__
        for _, method in _inspect.getmembers(cls, _inspect.isfunction):
            kp = getattr(method, "_key_patterns", None)
            if not kp:
                continue
            for keyword, _pattern, _params in kp:
                if _re.search(r"[\\^$*+?{}()\[\]|]", keyword):
                    continue
                if len(keyword) >= 2:
                    table[keyword] = name
    return table


def _is_parser_enabled(platform: str) -> bool:
    """平台启用判定 (r9: 与 enabled 列表收敛, 无配置 → 全部启用).

    优先级: platforms.items.enabled 勾选 → 旧模板 enable → True.
    """
    try:
        from bridge.adapter import enabled_platforms

        _en = enabled_platforms()
        if _en is not None:
            return platform.lower() in _en
        from bridge.adapter import platform_cfg as _platform_cfg

        _pc = _platform_cfg(platform)
        if "enable" in _pc:
            return bool(_pc["enable"])
        # 显式语义: 未配置勾选 → 全部启用 (设计默认)
        return True
    except Exception:
        return True
