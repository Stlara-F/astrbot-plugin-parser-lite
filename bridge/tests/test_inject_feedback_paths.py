"""注入反馈 + 路径统一 + options 纯字符串测试.

覆盖:
1. 注入反馈机制: 成功/失败报告写入 inject_report, 失败不阻断 (返回 [])
2. 路径统一: PARSER_LITE_BASE_DIR 单一来源, 默认与上游一致 (cwd/.parser-lite)
3. options 纯字符串: 杜绝 AstrBot 前端 [object Object] 占位
4. 模块状态目录统一 (disabled_groups 不再依赖 __file__)
"""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge import inject  # noqa: E402
from bridge.paths import get_base_dir, state_dir  # noqa: E402


def test_inject_report_structure():
    """inject_report 含成功/失败/注入项/版本字段."""
    assert set(inject.inject_report) >= {
        "last_ok",
        "last_error",
        "injected",
        "schema_version",
    }
    assert inject.inject_report["schema_version"] == inject.SCHEMA_VERSION


def test_inject_failure_feedback(tmp_path, monkeypatch):
    """注入失败: 报告记录原因, 返回 [] 不阻断加载."""
    schema_f = tmp_path / "schema.json"
    flag_f = tmp_path / ".injected"
    schema_f.write_text("{}", encoding="utf-8")

    def _boom(*a, **k):
        raise RuntimeError("simulated inject failure")

    monkeypatch.setattr(inject, "up_config", _boom)
    result = inject.inject_dynamic_options_static(schema_f, flag_f)
    assert result == []
    assert inject.inject_report["last_ok"] is False
    assert "simulated inject failure" in inject.inject_report["last_error"]


def test_inject_success_feedback(tmp_path, monkeypatch):
    """注入成功: 报告 last_ok=True + 注入项记录."""
    schema_f = tmp_path / "schema.json"
    flag_f = tmp_path / ".injected"
    schema_f.write_text("{}", encoding="utf-8")
    result = inject.inject_dynamic_options_static(schema_f, flag_f)
    assert isinstance(result, list)
    assert inject.inject_report["last_ok"] is True
    assert inject.inject_report["last_error"] == ""
    # 注入写回 schema + 版本标记
    assert schema_f.exists()
    assert flag_f.read_text(encoding="utf-8").strip() == str(inject.SCHEMA_VERSION)


def test_base_dir_env_priority(monkeypatch):
    """PARSER_LITE_BASE_DIR 优先于默认值."""
    monkeypatch.setenv("PARSER_LITE_BASE_DIR", "C:/tmp/envbase")
    assert get_base_dir() == Path("C:/tmp/envbase").resolve()


def test_base_dir_default_matches_upstream(monkeypatch):
    """默认 base_dir = cwd/.parser-lite (与上游 src/config.py 一致)."""
    monkeypatch.delenv("PARSER_LITE_BASE_DIR", raising=False)
    monkeypatch.chdir(_ROOT)
    assert get_base_dir() == (_ROOT / ".parser-lite").resolve()


def test_state_dir_under_base_dir(monkeypatch):
    """状态目录 = base_dir/parser_lite (单一来源)."""
    monkeypatch.setenv("PARSER_LITE_BASE_DIR", str(_ROOT / ".parser-lite-test-state"))
    assert state_dir() == (_ROOT / ".parser-lite-test-state" / "parser_lite").resolve()


def test_no_object_options_in_inject():
    """inject.py 无 {value,label} 对象 options (AstrBot 按字符串渲染)."""
    src = (_ROOT / "bridge" / "inject.py").read_text(encoding="utf-8")
    assert '{"value": ' not in src
    assert '"options": [{' not in src


def test_disabled_groups_no_file_dependency():
    """disabled_groups 路径不依赖 __file__ (统一状态目录)."""
    core_src = (_ROOT / "bridge" / "core.py").read_text(encoding="utf-8")
    assert "_DISABLED_GROUPS_FILE" in core_src
    assert (
        "os.path.dirname(os.path.abspath(__file__))"
        not in core_src.split("_load_disabled_groups")[0].split("def _get_logger")[-1]
    )
