"""导入完整性检查: 所有 from bridge.* import 的符号必须存在.

防止删除死代码时遗漏 import 引用 (曾致插件加载失败:
cannot import name '_extract_config_value' from 'bridge.core').
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _collect_from_imports(path: Path) -> list[tuple[str, str, str]]:
    """提取文件中所有 from X import a, b 语句."""
    results = []
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bridge"):
            for alias in node.names:
                if alias.name != "*":
                    results.append((str(path), node.module, alias.name))
    return results


def test_bridge_imports_exist():
    """main.py 与 bridge/*.py 中 from bridge.* import 的符号均存在."""
    files = [p for p in _ROOT.glob("**/*.py") if p.is_file()]
    missing = []
    for f in files:
        for _f, module, name in _collect_from_imports(f):
            mod_path = module.replace(".", "/")
            mod_file = _ROOT / f"{mod_path}.py"
            if not mod_file.exists():
                missing.append(f"{_f}: 模块 {module} 不存在")
                continue
            src = mod_file.read_text(encoding="utf-8")
            tree = ast.parse(src, str(mod_file))
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            defined |= {t.id for n in ast.walk(tree)
                        if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name)}
            defined |= {n.target.id for n in ast.walk(tree)
                        if isinstance(n, ast.AnnAssign)
                        and isinstance(n.target, ast.Name)}
            if name not in defined:
                missing.append(f"{_f}: {module}.{name} 不存在")
    assert not missing, "\n".join(missing)


def test_removed_symbols_not_imported():
    """已删除的符号 (合并死代码) 不应再被 import."""
    main_py = _ROOT / "main.py"
    text = main_py.read_text(encoding="utf-8")
    for removed in ("_extract_config_value", "_resolve_raw_addr", "_schema_proxy_cache"):
        assert removed not in text, f"已删除符号 {removed} 仍被引用"
