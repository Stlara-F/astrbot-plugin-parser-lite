"""配置读取与 template_list 兼容测试 — 覆盖嵌套 items 与可增删列表格式.

- parsers.items 嵌套读取 (走代理/ Cookie)
- platforms template_list 读取 (每平台配置)
- push template_list 订阅解析
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.core as core  # noqa: E402


def test_read_cfg_dot_path():
    """read_cfg 支持点路径嵌套 (配置读取统一入口)."""
    from bridge.cfg import read_cfg

    src = {"delay_send": {"enabled": True, "timeout_sec": 300}, "push": [{"uid": "1"}]}
    assert read_cfg(src, "delay_send.enabled") is True
    assert read_cfg(src, "delay_send.timeout_sec") == 300
    assert read_cfg(src, "delay_send.nope", 42) == 42
    assert read_cfg(src, "push") == [{"uid": "1"}]
    assert read_cfg(src, "missing", "d") == "d"
    assert read_cfg(None, "x", "d") == "d"
    assert read_cfg(src, "delay_send.enabled.deep", "d") == "d"


def test_read_cfg_zero_valid():
    """0 是合法值 (TTL=0 禁用), 不被回退覆盖."""
    from bridge.cfg import read_cfg

    assert read_cfg({"plite_dedup_ttl": 0}, "plite_dedup_ttl", 60) == 0


def test_load_parsers_config_nested_items():
    """AstrBot 配置 {parsers: {items: {proxied}}} → 读取时应解包 items."""
    core.BridgeConfig._source = {
        "parsers": {"items": {"proxied": ["bilibili"], "cookies": []}},
    }
    cfg = core._load_parsers_config()
    assert cfg.get("proxied") == ["bilibili"]


def test_load_parsers_config_flat():
    """兼容旧扁平格式 {parsers: {proxied: [...]}}."""
    core.BridgeConfig._source = {"parsers": {"proxied": ["bilibili"]}}
    cfg = core._load_parsers_config()
    assert cfg.get("proxied") == ["bilibili"]


def test_use_proxy_for_nested():
    core.BridgeConfig._source = {
        "parsers": {"items": {"proxied": ["bilibili"]}},
    }
    assert core._use_proxy_for("bilibili") is True
    assert core._use_proxy_for("zhihu") is False


def test_get_cookies_template_list():
    """cookies 为可增删列表 [{platform, cookie}]."""
    core.BridgeConfig._source = {
        "parsers": {"items": {"cookies": [
            {"platform": "bilibili", "cookie": "SESSDATA=abc"},
            {"platform": "zhihu", "cookie": "z_c0=xyz"},
        ]}},
    }
    assert core._get_cookies_for("bilibili") == {"Cookie": "SESSDATA=abc"}
    assert core._get_cookies_for("douyin") == {}


def test_get_cookies_legacy_json():
    """兼容旧 JSON 字符串格式."""
    core.BridgeConfig._source = {
        "parsers": {"items": {"cookies": '{"bilibili": "SESSDATA=old"}'}},
    }
    assert core._get_cookies_for("bilibili") == {"Cookie": "SESSDATA=old"}


def test_platform_cfg_template_list():
    """platforms template_list: [{platform, enable, use_proxy, cookies}]."""
    core.BridgeConfig._source = {
        "platforms": [
            {"platform": "bilibili", "enable": True, "use_proxy": True, "cookies": "ck1"},
            {"platform": "zhihu", "enable": False},
        ],
    }
    assert core._platform_cfg("bilibili")["use_proxy"] is True
    assert core._platform_cfg("zhihu")["enable"] is False
    assert core._platform_cfg("douyin") == {}


def test_platform_cfg_legacy_dict():
    """兼容旧 dict 格式 {platform: {...}}."""
    core.BridgeConfig._source = {
        "platforms": {"bilibili": {"enable": True}},
    }
    assert core._platform_cfg("bilibili")["enable"] is True


def test_platform_enable_priority():
    """platforms.enable 优先于 disabled_platforms."""
    core.BridgeConfig._source = {
        "platforms": [{"platform": "bilibili", "enable": False}],
    }
    # _is_parser_enabled 读取 platforms.enable
    assert core._is_parser_enabled("bilibili") is False
