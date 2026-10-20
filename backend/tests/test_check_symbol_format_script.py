"""Characterization tests for scripts/check_symbol_format.py check_file()."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_symbol_format.py"

SOURCE = """\
a = s.replace("NSE:", "")
# b = s.replace("NSE:", "")
'''c = s.replace("-EQ", "")'''
"d.replace('-EQ', '')"
"e" + x.replace('-EQ', '') if y else z
"f = s.split(':')"
    return s.split(":")
g = s.replace('-EQ', '').split(':')
"""


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_symbol_format_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_file_reports_code_lines_only(checker: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text(SOURCE, encoding="utf-8")

    found = [(v.code, v.line, v.column, v.context) for v in checker.check_file(path)]

    assert found == [
        ("SYM001", 1, 6, 'a = s.replace("NSE:", "")'),
        ("SYM002", 8, 6, "g = s.replace('-EQ', '').split(':')"),
        ("SYM003", 6, 7, "\"f = s.split(':')\""),
        ("SYM003", 7, 13, 'return s.split(":")'),
        ("SYM003", 8, 25, "g = s.replace('-EQ', '').split(':')"),
    ]


@pytest.mark.parametrize(
    "relative",
    [
        "symbol_master.py",
        "check_symbol_format.py",
        "test_things.py",
        "tests/helpers.py",
        "test_violations/bad.py",
    ],
)
def test_check_file_skips_exempt_paths(checker: ModuleType, tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SOURCE, encoding="utf-8")

    assert checker.check_file(path) == []


def test_check_file_unreadable(checker: ModuleType, tmp_path: Path) -> None:
    assert checker.check_file(tmp_path / "missing.py") == []
