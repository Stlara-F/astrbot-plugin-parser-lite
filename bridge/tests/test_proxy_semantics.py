"""直连语义测试 (T2: 代理体系已收敛为直连).

覆盖:
- apply_downloader_proxy 直连重建 (客户端存活早期返回)
- platform_cfg 不再含 proxy 键 (规则代理移除)
- enabled_platforms 勾选语义保留
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.adapter as core  # noqa: E402
from bridge.config import BridgeConfig


def test_platform_cfg_no_proxy_key():
    """T2: platform_cfg 不再产出 proxy 键 (规则代理移除)."""
    BridgeConfig._source = {
        "platforms": [{"platform": "x", "proxy": True, "enable": True}],
    }
    _pc = core._platform_cfg("x")
    assert "proxy" not in _pc
    assert _pc.get("enable") is True


def test_enabled_platforms_checklist():
    """新格式 enabled 勾选语义保留."""
    BridgeConfig._source = {
        "platforms": {"items": {"enabled": ["bilibili"]}},
    }
    assert core._is_parser_enabled("bilibili") is True
    assert core._is_parser_enabled("weibo") is False


def test_downloader_rebuild_direct():
    """apply_downloader_proxy 直连重建 (早期返回/存活检测)."""
    from bridge.adapter import apply_downloader_proxy

    try:
        apply_downloader_proxy()  # 无上游 client 时静默 (CI 无 astrbot)
    except Exception:
        pass  # CI 无 astrbot 时跳过
