"""The frozen VCP oracle HAT took from legacy code stays faithful while the detector exists (K1).

HAT's PR 6 characterization test reads `tests/hat/fixtures/vcp_oracle.json` instead of the legacy
VCP detector. These checks compare the file with its source. They are legacy tests: delete this
file with the VCP detector (K5).
"""

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from app.services.vcp import pattern

ORACLE = json.loads(
    (Path(__file__).parent / "hat" / "fixtures" / "vcp_oracle.json").read_text(encoding="utf-8")
)


def test_oracle_constants_match_the_detector() -> None:
    assert ORACLE["constants"] == {
        "min_history_bars": pattern.MIN_HISTORY_BARS,
        "min_rs_rating": pattern.MIN_RS_RATING,
        "stop_offset_pct": pattern.STOP_OFFSET_PCT,
    }


@pytest.mark.parametrize("case", ORACLE["cases"], ids=lambda c: c["name"])
def test_oracle_case_matches_the_detector(case: dict) -> None:
    result = pattern.analyze_pattern(pd.DataFrame(case["bars"]), case["rs_rating"])
    got = None if result is None else json.loads(json.dumps(dataclasses.asdict(result)))
    assert got == case["expected"]


def test_oracle_rs_map_matches_the_detector() -> None:
    frames = {
        name: pd.DataFrame({"close_ref": spec["close_ref"]})
        for name, spec in ORACLE["rs_map"]["symbols"].items()
    }
    assert pattern.compute_rs_map(frames) == ORACLE["rs_map"]["expected"]
