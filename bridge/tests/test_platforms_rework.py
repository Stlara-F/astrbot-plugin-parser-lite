"""平台配置重构测试 — 勾选列表 + 动态 cookie + 排序 + 翻译 + OneBot11 反馈.

覆盖 (配置重构 6 项):
1. platforms 统一勾选列表 (enabled/proxied 替代 27 平台模板)
2. cookie 动态源: 源码 plite_*_ck 字段扫描 → cookies 动态模板
3. 排序: 注入的 standalone 源码配置项在前, 扩展自实现配置项在后
4. OneBot11 段结构反馈 (type/data 数组格式)
5. 翻译列表: 已知 key 翻译, 未知 key 原样 (未翻译状态)
6. 新格式读取 + 旧 27 模板格式迁移兼容
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge import inject  # noqa: E402
from bridge.core import BridgeConfig, _is_parser_enabled  # noqa: E402
from bridge.proxy import (  # noqa: E402
    _platforms_block,
    cookies_entries,
    enabled_platforms,
    get_cookies_for,
    sync_cookies_to_upstream,
)
from bridge.send import _onebot11_segments  # noqa: E402


def _src():
    return (_ROOT / "bridge" / "inject.py").read_text("utf-8")


def _set_cfg(data: dict):
    BridgeConfig._source = data


def test_new_checklist_format_enabled():
    """新格式: platforms.items.enabled 勾选 → 启用判定."""
    _set_cfg(
        {
            "platforms": {
                "items": {
                    "enabled": ["bilibili", "zhihu"],
                    "cookies": [],
                }
            }
        }
    )
    assert enabled_platforms() == {"bilibili", "zhihu"}
    assert _is_parser_enabled("bilibili") is True
    assert _is_parser_enabled("weibo") is False


def test_new_checklist_format_enabled_dict_values():
    """勾选列表支持 {"value": "x"} 形式 (AstrBot options)."""
    _set_cfg({"platforms": {"items": {"enabled": [{"value": "bilibili"}]}}})
    assert _is_parser_enabled("bilibili") is True
    assert _is_parser_enabled("weibo") is False


def test_new_checklist_cookies_entries():
    """新格式: platforms.items.cookies 动态条目 → get_cookies_for."""
    _set_cfg(
        {
            "platforms": {
                "items": {
                    "cookies": [
                        {"platform": "bilibili", "cookie": "SESSDATA=abc"},
                        {"platform": "zhihu", "cookie": "z_c0=xyz"},
                    ]
                }
            }
        }
    )
    assert get_cookies_for("bilibili") == {"Cookie": "SESSDATA=abc"}
    assert get_cookies_for("zhihu") == {"Cookie": "z_c0=xyz"}
    assert get_cookies_for("douyin") == {}
    assert len(cookies_entries()) == 2


def test_astrbot_flattened_platforms():
    """AstrBot 生成配置把 object 展平: platforms.enabled 直接顶层 (无 items)."""
    _set_cfg(
        {
            "platforms": {
                "enabled": ["bilibili", "zhihu"],
                "cookies": [{"platform": "bilibili", "cookie": "SESSDATA=f"}],
            }
        }
    )
    assert _platforms_block()["items"]["enabled"] == ["bilibili", "zhihu"]
    assert _is_parser_enabled("bilibili") is True
    assert _is_parser_enabled("weibo") is False
    assert get_cookies_for("bilibili") == {"Cookie": "SESSDATA=f"}


def test_legacy_27_template_migration():
    """旧 27 平台模板格式 → 迁移兼容读取."""
    _set_cfg(
        {
            "platforms": [
                {
                    "platform": "bilibili",
                    "enable": True,
                    "proxy": True,
                    "cookies": "ck1",
                },
                {"platform": "zhihu", "enable": False},
            ]
        }
    )
    assert _is_parser_enabled("bilibili") is True
    assert _is_parser_enabled("zhihu") is False
    assert get_cookies_for("bilibili") == {"Cookie": "ck1"}


def test_cookie_dynamic_source_scan():
    """cookie 动态源: 注入层扫描源码 plite_*_ck 字段."""
    src = _src()
    assert 'fname.startswith("plite_") and _fname.endswith("_ck")' in src
    assert "cookies" in src
    assert "template_list" in src


def test_ordering_upstream_first_extension_last():
    """排序: 注入的 standalone 源码配置项在前, 扩展自实现配置项在后."""
    src = _src()
    assert "上游模型序" in src or "standalone 源码实现配置项在前" in src
    # _BRIDGE_FIELDS 声明序 (扩展) 在排序逻辑中位于上游之后
    bf_start = src.find("_BRIDGE_FIELDS: list[dict] = [")
    order_start = src.find("_known_order = [")
    assert bf_start != -1
    assert order_start != -1
    assert order_start > bf_start


def test_translation_known_and_unknown():
    """翻译列表: 已知 key 翻译, 未知 key 原样 (未翻译状态进入配置)."""
    assert inject.tr("features") == "功能开关"
    assert inject.tr("send_strategy") == "发送策略"
    assert inject.tr("plite_max_size") == "资源最大大小MB"
    # 未翻译: 原样返回
    assert inject.tr("plite_future_new_field") == "plite_future_new_field"
    # 翻译表存在且有序
    assert len(inject.TRANSLATIONS) > 20


def test_onebot11_segments_structure():
    """OneBot11 段数组: {"type": ..., "data": {...}} 值均为字符串."""

    class FakeSeg:
        type = "image"
        file = "base64://abc=="
        text = None
        url = None

    class FakePlain:
        type = "text"
        text = "hello"
        file = None
        url = None

    segs = _onebot11_segments([FakePlain(), FakeSeg()])
    assert segs == [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "image", "data": {"file": "base64://abc=="}},
    ]
    assert all(isinstance(v.get("data"), dict) for v in segs)


def test_sync_cookies_to_upstream_missing_upstream():
    """无上游时 sync_cookies_to_upstream 静默跳过 (不抛异常)."""
    _set_cfg(
        {
            "platforms": {
                "items": {
                    "cookies": [
                        {"platform": "bilibili", "cookie": "SESSDATA=sync"},
                    ]
                }
            }
        }
    )
    sync_cookies_to_upstream()  # 无 astrbot/无上游 → 静默
