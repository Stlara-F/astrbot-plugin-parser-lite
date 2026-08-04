"""debounce 模块测试 — 与被测代码同目录 (bridge/tests/test_debounce.py)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.debounce import Debouncer, debounce_key, make_debouncer  # noqa: E402


def _debouncer():
    tmp = tempfile.mkdtemp()
    return Debouncer(os.path.join(tmp, "db.json"))


def test_should_parse_first_time():
    d = _debouncer()
    assert d.should_parse("g1:https://a.com", 60) is True


def test_should_parse_blocks_within_window():
    d = _debouncer()
    key = "g1:https://a.com"
    assert d.should_parse(key, 60) is True
    assert d.should_parse(key, 60) is False


def test_should_parse_allows_after_window():
    d = _debouncer()
    key = "g1:https://a.com"
    assert d.should_parse(key, 0) is True  # window=0 → 立即允许


def test_rollback_allows_retry():
    d = _debouncer()
    key = "g1:https://a.com"
    assert d.should_parse(key, 60) is True
    assert d.should_parse(key, 60) is False  # 防抖命中
    d.rollback(key)  # 失败回滚
    assert d.should_parse(key, 60) is True  # 允许重试


def test_mark_success_sets_timestamp():
    d = _debouncer()
    key = "g1:https://a.com"
    d.mark_success(key)
    assert d.should_parse(key, 60) is False


def test_debounce_key_format():
    assert debounce_key("g123", "https://a.com") == "g123:https://a.com"


def test_make_debouncer_persists():
    tmp = tempfile.mkdtemp()
    d = make_debouncer(tmp)
    d.mark_success("k1")
    d2 = make_debouncer(tmp)  # 重新加载 → 持久化生效
    assert d2.should_parse("k1", 60) is False
