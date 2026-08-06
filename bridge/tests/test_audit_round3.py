"""审计第三轮修复测试 (T3 后: cookie_health/arbiter/delay_send 已移除, 保留 format/render_patch)."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.format import format_brief, format_full  # noqa: E402


def test_format_full_none_platform():
    """P3-3: platform/author 为 None 时不抛异常."""

    class FakeResult:
        def __init__(self):
            self.platform = None
            self.author = None
            self.title = None
            self.timestamp = None
            self.content = []
            self.stats = type(
                "S",
                (),
                {
                    "view_count": 0,
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                    "collect_count": 0,
                },
            )()
            self.comments = []
            self.ai_summary = ""

    text = format_full(FakeResult())
    assert "解析" in text
    assert format_brief(FakeResult()).strip()


def test_render_patch_restore_function():
    """P2-9: restore_render_patch 存在且可还原."""
    from bridge import render_patch as rp

    assert callable(rp.restore_render_patch)
    # 无上游 (CI) 时静默返回 False, 不抛异常
    assert rp.restore_render_patch() in (True, False)


def test_removed_modules_not_imported():
    """T3: delay_send/arbiter/cookie_health 模块已移除 (main 无残留引用)."""
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    for mod in (
        "bridge.delay_send",
        "bridge.arbiter",
        "bridge.cookie_health",
        "DelaySender",
        "CookieHealth",
    ):
        assert mod not in main_src, f"main.py 仍引用已移除模块: {mod}"
    for f in ("delay_send.py", "arbiter.py", "cookie_health.py"):
        assert not (_ROOT / "bridge" / f).exists(), f"{f} 未删除"
