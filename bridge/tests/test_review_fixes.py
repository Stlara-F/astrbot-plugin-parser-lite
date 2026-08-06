"""Review 修复专项测试: 写节流/原子落盘 + 审计回归."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.state_store import JsonStateStore  # noqa: E402


def test_state_store_atomic_write(tmp_path):
    """JsonStateStore: 原子落盘 (tmp + os.replace), 无 .tmp 残留."""
    store = JsonStateStore(tmp_path / "st.json", flush_every=1)
    store.update(lambda d: d.setdefault("n", 0) or d.update(n=1))
    store.flush()
    assert (tmp_path / "st.json").exists()
    assert not (tmp_path / "st.json.tmp").exists()
    loaded = JsonStateStore(tmp_path / "st.json").data
    assert loaded["n"] == 1


def test_state_store_coalescing(tmp_path):
    """写节流: 未达阈值不落盘 (内存更新即时, 落盘延迟)."""
    store = JsonStateStore(tmp_path / "st2.json", flush_every=10, flush_interval=60)
    for i in range(5):
        store.update(lambda d, i=i: d.update(n=i))
    assert not (tmp_path / "st2.json").exists()  # 5 < 10, 未落盘
    store.flush()
    assert (tmp_path / "st2.json").exists()
    assert JsonStateStore(tmp_path / "st2.json").data["n"] == 4


def test_state_store_concurrent_updates(tmp_path):
    """并发更新不丢失 (锁保护)."""
    import threading

    store = JsonStateStore(tmp_path / "st3.json", flush_every=100, flush_interval=60)
    threads = []
    for i in range(20):
        t = threading.Thread(
            target=lambda i=i: store.update(lambda d, i=i: d.__setitem__(f"k{i}", i))
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    store.flush()
    loaded = JsonStateStore(tmp_path / "st3.json").data
    assert len(loaded) == 20


# ── 审计报告第二轮修复测试 ────────────────────────────────────────────────


def test_doctor_class_model_fields():
    """P0-2: doctor 不得实例访问 model_fields (pydantic 2.11+ deprecation)."""
    src = (_ROOT / "bridge" / "doctor.py").read_text(encoding="utf-8")
    assert "type(cfg).model_fields" in src
    assert "cfg.model_fields" not in src


def test_bool_annotation_helper():
    """P1-7: bool 判定兼容 bool | None / bool | None."""
    from bridge.inject import is_bool_field

    class _F:
        def __init__(self, ann):
            self.annotation = ann

    assert is_bool_field(_F(bool)) is True
    assert is_bool_field(_F(bool | None)) is True
    assert is_bool_field(_F(bool | int)) is True
    assert is_bool_field(_F(int)) is False
    assert is_bool_field(_F(str | None)) is False


def test_no_on_url_auto():
    """P2-2: on_url_auto 死代码已删除 (装饰器注册也不存在)."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "on_url_auto" not in src


def test_no_group_message_zero():
    """P2-5: 移除无效 GroupMessage:0 发送 (通知仅日志)."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "aiocqhttp:GroupMessage:0" not in src
