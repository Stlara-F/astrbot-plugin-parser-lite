"""doctor 自检模块测试 — 结构化结果、可观测、错误显式返回.

验证:
- CheckResult 结构 (ok/detail/error/duration/warn)
- run_checks 聚合全部检查项
- summarize 计数与失败项提取
- render_text 人类可读输出
- 单检查异常 → 显式 error, 不崩溃
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.doctor as doctor  # noqa: E402


def test_check_result_structure():
    r = doctor.CheckResult("test", True, detail="ok detail", duration=1.5)
    d = r.to_dict()
    assert d["name"] == "test"
    assert d["ok"] is True
    assert d["detail"] == "ok detail"
    assert d["error"] == ""
    assert d["duration"] == 1.5


def test_run_check_ok():
    async def fn():
        return True, "fine", False

    r = asyncio.run(doctor._run_check("t", fn))
    assert r.ok is True
    assert r.detail == "fine"
    assert r.error == ""


def test_run_check_exception_returns_error():
    """检查函数抛异常 → CheckResult 显式 error, 不冒泡崩溃."""

    async def fn():
        raise RuntimeError("boom")

    r = asyncio.run(doctor._run_check("t", fn))
    assert r.ok is False
    assert "boom" in r.error


def test_run_check_warn():
    async def fn():
        return False, "missing", True

    r = asyncio.run(doctor._run_check("t", fn))
    assert r.ok is False
    assert r.warn is True


@pytest.mark.asyncio
async def test_run_checks_all():
    """registry 全部检查项运行不崩溃, 结果含时间戳."""
    results = await doctor.run_checks()
    names = [r.name for r in results]
    assert len(results) == len(doctor.CHECK_REGISTRY)
    for r in results:
        assert r.duration >= 0
    # 关键检查项存在
    for required in ("config", "parsers", "coverage", "network", "render", "schema"):
        assert required in names


def test_summarize_counts():
    results = [
        doctor.CheckResult("a", True),
        doctor.CheckResult("b", True),
        doctor.CheckResult("c", False, warn=True),
        doctor.CheckResult("d", False, error="bad"),
    ]
    s = doctor.summarize(results)
    assert s["total"] == 4
    assert s["ok"] == 2
    assert s["warn"] == 1
    assert s["failed"] == 1
    assert s["failed_items"] == ["d"]
    assert s["all_ok"] is False


def test_summarize_all_ok():
    results = [doctor.CheckResult("a", True), doctor.CheckResult("b", True)]
    s = doctor.summarize(results)
    assert s["all_ok"] is True
    assert s["failed_items"] == []


def test_render_text_includes_error():
    results = [
        doctor.CheckResult("config", True, detail="10 fields"),
        doctor.CheckResult("network", False, error="connection refused"),
    ]
    s = doctor.summarize(results)
    text = doctor.render_text(results, s)
    assert "[OK] config" in text
    assert "[FAIL] network" in text
    assert "connection refused" in text
    assert "1/2 OK" in text


@pytest.mark.asyncio
async def test_check_config_real():
    """实际运行 config 检查 (无 astrbot 环境应显式失败不崩溃)."""
    r = await doctor.check_config()
    assert isinstance(r, tuple)
    assert len(r) == 3


@pytest.mark.asyncio
async def test_check_coverage_real():
    """实际运行覆盖率检查: 27 平台多数注册."""
    _ok, detail, _warn = await doctor.check_coverage()
    assert "27" in detail or "26" in detail  # 平台数合理


def test_to_json_serializable():
    """JSON 输出: 机器可解析, 含时间戳."""
    results = [
        doctor.CheckResult("a", True, detail="fine"),
        doctor.CheckResult("b", False, error="boom"),
    ]
    s = doctor.summarize(results)
    text = doctor.to_json(results, s)
    import json

    payload = json.loads(text)
    assert payload["summary"]["failed"] == 1
    assert payload["checks"][1]["error"] == "boom"
    assert "timestamp" in payload


def test_save_snapshot_all_ok_no_file(tmp_path):
    """全部 OK → 不落盘 (无失败无需快照)."""
    results = [doctor.CheckResult("a", True)]
    s = doctor.summarize(results)
    target = str(tmp_path / "snap.json")
    assert doctor.save_snapshot(results, s, target) is None
    assert not Path(target).exists()


def test_save_snapshot_writes_on_fail(tmp_path):
    """有失败 → 落盘 JSON 快照 (错误显式持久化)."""
    results = [
        doctor.CheckResult("a", True),
        doctor.CheckResult("net", False, error="conn refused"),
    ]
    s = doctor.summarize(results)
    target = str(tmp_path / "snap.json")
    path = doctor.save_snapshot(results, s, target)
    assert path == target
    assert Path(target).exists()
    import json

    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    assert payload["checks"][1]["name"] == "net"
    assert payload["checks"][1]["ok"] is False


def test_doctor_command_registered():
    """cmd_doctor 命令别名注册 (parse_doctor + parser_doctor) — 静态检查注册行.

    不 import main (依赖 astrbot, CI 不可用), 检查 filter.command 注册声明.
    """
    main_py = Path(_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'filter.command("parse_doctor")' in main_py
    assert 'filter.command("parser_doctor")' in main_py
    assert "cmd_doctor" in main_py
