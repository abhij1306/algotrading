"""Text parsers of the phase-1 data pipeline (data_platform/pipelines/phase1_build.py)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PIPELINE_PATH = (
    Path(__file__).resolve().parents[2] / "data_platform" / "pipelines" / "phase1_build.py"
)


@pytest.fixture(scope="module")
def phase1() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase1_build_under_test", PIPELINE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("RELIANCE Reliance Industries Ltd. 1,234 9.87", None),
        ("RELIANCE Reliance Industries Ltd. 1234 9.87", ("RELIANCE", 9.87)),
        ("M&M Mahindra & Mahindra Ltd. 12.5 -0.25", ("M&M", -0.25)),
        ("BAJAJ-AUTO Bajaj Auto Ltd. 3 1", ("BAJAJ-AUTO", 1.0)),
        ("RELIANCE 1234 9.87", None),
        ("Reliance Reliance Industries 1234 9.87", None),
        ("RELIANCE Reliance Industries Ltd. 1234 n/a", None),
        ("", None),
    ],
)
def test_parse_nifty50_weight_line(
    phase1: ModuleType, line: str, expected: tuple[str, float] | None
) -> None:
    assert phase1.parse_nifty50_weight_line(line) == expected


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("Bonus 1:1", ("bonus", 2.0)),
        ("bonus issue 3 : 2 and dividend 5:1", ("bonus", 2.5)),
        ("Dividend 2:1 then bonus 1:4", ("bonus", 1.25)),
        ("bonus\n1:1", (None, None)),
        ("bonus 1\n: 2", ("bonus", 1.5)),
        ("bonus\nbonus 4:1", ("bonus", 5.0)),
        ("bonus 1:0", (None, None)),
        ("Face value split from Rs 10 to Rs 2 (FV)", ("split", 5.0)),
        ("Dividend Rs 5", (None, None)),
    ],
)
def test_parse_corp_action_factor(
    phase1: ModuleType, purpose: str, expected: tuple[str | None, float | None]
) -> None:
    assert phase1.Phase1Builder._parse_corp_action_factor(purpose) == expected
