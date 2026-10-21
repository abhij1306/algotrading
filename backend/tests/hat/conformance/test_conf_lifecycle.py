"""CONF check HAT-EXEC-040: the states and transitions the engine holds equal the table of
07 section 4, row for row."""

import re

from hat_conf_spec import SPEC_DIR

from app.hat.engine import lifecycle
from app.hat.models.runtime import TRADE_STATES


def _section() -> str:
    text = (SPEC_DIR / "07-execution-and-brokers.md").read_text(encoding="utf-8")
    start = text.index("## §4 Trade lifecycle")
    return text[start : text.index("#### HAT-EXEC-040", start)]


def _rows(section: str, header: str) -> list[list[str]]:
    lines = section[section.index(header) :].splitlines()[2:]
    rows = []
    for line in lines:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
    return rows


def test_lifecycle_transitions_match_spec() -> None:
    section = _section()
    states = [r[1] for r in _rows(section, "| # | State | Kind |")]
    kinds = {r[1]: r[2].split(" ")[0] for r in _rows(section, "| # | State | Kind |")}
    assert len(states) == 11
    assert set(states) == set(lifecycle.STATES) == set(TRADE_STATES)
    assert {s for s, k in kinds.items() if k == "ending"} == set(lifecycle.ENDINGS)
    spec_pairs = set()
    for src, dst, *_ in _rows(section, "| From | To | Driving event | Condition |"):
        if src == "(none)":
            continue
        spec_pairs |= {(re.sub(r"\s+", "", s), dst) for s in src.split(",")}
    code_pairs = {(src, dst) for (src, _), dst in lifecycle.TRANSITIONS.items()}
    assert code_pairs == spec_pairs
