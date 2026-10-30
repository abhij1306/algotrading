"""The S1 setups diagnosis tool (`scripts/research/s1_setups_diag.py`): each leave-one-out rule
removes only its rule, and the report's seams (a trade's legs, the extra entries, the stats)."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from types import ModuleType

import pytest

from app.paths import PROJECT_ROOT


def _script() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "research" / "s1_setups_diag.py"
    spec = importlib.util.spec_from_file_location("s1_setups_diag", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


d = _script()
SHADOW = d.rule_sets(d.Rule("none", "nothing"))


def _books(sets: list[str]) -> list[dict]:
    return json.loads(sets[0].removeprefix("playbooks="))


def _criteria(books: list[dict], name: str) -> list[dict]:
    return next(b for b in books if b["name"] == name)["setup"]["criteria"]


def _params(books: list[dict], name: str, detector: str) -> dict:
    return next(c for c in _criteria(books, name) if c["type"] == detector)["params"]


@pytest.mark.parametrize("rule", d.RULES, ids=lambda r: r.name)
def test_each_rule_changes_the_shadow_run(rule: object) -> None:
    sets = d.rule_sets(rule)
    assert sets != SHADOW
    # everything but the playbooks and the regime is the shadow run's
    assert [s for s in sets if not s.startswith(("playbooks=", "regime="))] == SHADOW[1:]


def test_rules_remove_only_their_rule() -> None:
    rules = {r.name: r for r in d.RULES}
    shadow = _books(SHADOW)
    no_tt = _books(d.rule_sets(rules["trend_template"]))
    assert [c["type"] for c in _criteria(no_tt, "vcp")][:2] == ["rs_rank", "book_vcp"]
    assert _params(no_tt, "primary_base", "book_primary_base") == {"sma50_min_history": None}
    dry = _books(d.rule_sets(rules["dry_up"]))
    for name in d.SETUPS:
        detector = f"book_{name}"
        assert _params(dry, name, detector) == d.DRY_OFF
        assert _criteria(dry, name)[:-5] == _criteria(shadow, name)[:-5]  # the gates are kept
    volume = _books(d.rule_sets(rules["breakout_volume"]))
    assert all([t["type"] for t in b["trigger"]] == ["price_breaks_level"] for b in volume)
    assert all(
        b["entry_filters"] == s["entry_filters"] for b, s in zip(volume, shadow, strict=True)
    )
    assert d.REGIME_OFF in d.rule_sets(rules["regime"])


def _row(trade: str, r: str, setup: str = "vcp", day: str = "2017-01-02") -> dict[str, str]:
    return {
        "trade_id": trade,
        "instrument_id": f"i{trade}",
        "entry_date": day,
        "setup_type": setup,
        "r_net": r,
    }


def test_trades_sum_their_legs_and_extra_is_by_entry() -> None:
    base = d.trades_of([_row("1", "1.5"), _row("1", "0.5"), _row("2", "-1")])
    assert [(t.key, t.r) for t in base] == [
        (("i1", "2017-01-02"), Decimal(2)),
        (("i2", "2017-01-02"), Decimal(-1)),
    ]
    variant = d.trades_of([_row("1", "2"), _row("3", "-0.5", "cheat_3c")])
    added, lost = d.entry_diff(base, variant)
    assert [t.key[0] for t in added] == ["i3"]
    assert [t.key[0] for t in lost] == ["i2"]
    assert d.swaps(added, lost) == 0
    # i2 lost on one date and taken on another (one setup per instrument swapped its setup)
    assert d.swaps([*added, *d.trades_of([_row("2", "1", day="2017-02-01")])], lost) == 1
    assert d.stats(added) == d.Stats(1, 0.0, -0.5)
    assert d.stats(base) == d.Stats(2, 50.0, 0.5)
    assert d.stats([]) == d.Stats(0, None, None)


def test_extra_setups_and_logged_runs() -> None:
    base = {"by_playbook": {"vcp": {"setup": 10}, "cheat": {"setup": 4}}}
    variant = {"by_playbook": {"vcp": {"setup": 15}, "cheat": {"setup": 4}}}
    assert d.extra_setups(base, variant) == {"vcp": 5, "cheat": 0}
    lines = [
        "noise",
        '{"label": "shadow", "run_id": "a", "status": "complete"}',
        '{"label": "dry_up", "run_id": null, "status": "refused"}',
    ]
    assert d.logged_runs(lines) == {"shadow": "a"}
