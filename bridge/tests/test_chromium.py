"""Chromium 统一编排测试 (r11: mock 内部步骤, 覆盖各分支).

- 验证通过 → 直接 ok
- 下载失败 → 系统库补齐 → 验证
- 非 root → 手动指引
- 最终失败 → ✗✗ 指引
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


@pytest.mark.asyncio
async def test_verify_ok_returns_started(monkeypatch):
    from bridge import chromium

    async def _fake_verify() -> bool:
        return True

    monkeypatch.setattr(chromium, "_verify", _fake_verify)
    ok, messages = await chromium.ensure_chromium(started_msg="已就绪")
    assert ok is True
    assert messages == ["已就绪"]


@pytest.mark.asyncio
async def test_download_fail_then_libs_ok(monkeypatch):
    from bridge import chromium

    async def _fake_verify() -> bool:
        return False

    async def _fake_download(browsers_path, messages) -> bool:
        messages.append("下载失败")
        return False

    async def _fake_libs(messages) -> bool:
        messages.append("系统库补齐")
        return True

    async def _fake_verify2() -> bool:
        return True

    state = {"calls": 0}

    async def _verify_seq() -> bool:
        state["calls"] += 1
        return state["calls"] > 1  # 第一次失败, 之后成功

    monkeypatch.setattr(chromium, "_verify", _verify_seq)
    monkeypatch.setattr(chromium, "_download_chromium", _fake_download)
    monkeypatch.setattr(chromium, "_install_system_libs", _fake_libs)
    ok, messages = await chromium.ensure_chromium()
    assert ok is True
    assert "下载失败" in messages
    assert "系统库补齐" in messages


@pytest.mark.asyncio
async def test_non_root_manual_guidance(monkeypatch):
    from bridge import chromium

    async def _fake_verify() -> bool:
        return False

    async def _fake_download(browsers_path, messages) -> bool:
        messages.append("下载完成")
        return True

    async def _fake_libs(messages) -> bool:
        messages.append("✗ 非 root 用户无法安装系统库, 请在容器/服务器以 root 运行")
        return False

    async def _verify_fail() -> bool:
        return False

    monkeypatch.setattr(chromium, "_verify", _verify_fail)
    monkeypatch.setattr(chromium, "_download_chromium", _fake_download)
    monkeypatch.setattr(chromium, "_install_system_libs", _fake_libs)
    ok, messages = await chromium.ensure_chromium()
    assert ok is False
    assert any("非 root" in m for m in messages)
    assert any("✗✗" in m for m in messages)


@pytest.mark.asyncio
async def test_total_failure_guidance(monkeypatch):
    from bridge import chromium

    async def _verify_fail() -> bool:
        return False

    async def _fake_download(browsers_path, messages) -> bool:
        messages.append("所有镜像失败")
        return False

    async def _fake_libs(messages) -> bool:
        messages.append("apt 也失败")
        return False

    monkeypatch.setattr(chromium, "_verify", _verify_fail)
    monkeypatch.setattr(chromium, "_download_chromium", _fake_download)
    monkeypatch.setattr(chromium, "_install_system_libs", _fake_libs)
    ok, messages = await chromium.ensure_chromium()
    assert ok is False
    assert any("✗✗" in m for m in messages)


@pytest.mark.asyncio
async def test_install_system_libs_root_apt_fallback(monkeypatch):
    """root + install-deps 失败 → apt-get 回退 (mock subprocess)."""
    from bridge import chromium

    class FakeProc:
        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return b"", b"err"

    procs = iter(
        [FakeProc(1), FakeProc(0), FakeProc(0)]
    )  # deps 失败, apt update, apt install

    async def fake_subprocess_exec(*a, **kw):
        return next(procs)

    monkeypatch.setattr("bridge.core._detect_missing_libs", lambda: ["libnss3"])
    monkeypatch.setattr(chromium, "_is_root", lambda: True)
    monkeypatch.setattr(
        chromium.asyncio, "create_subprocess_exec", fake_subprocess_exec
    )
    messages: list[str] = []
    ok = await chromium._install_system_libs(messages)
    assert ok is True
    assert any("install-deps 失败" in m for m in messages)
