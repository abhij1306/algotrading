"""Sweep discipline (docs/backtests/process.md, rules 2 and 3): what a variant changes against
its base, counted in knobs, and the guards of a hold-out run.

A run's knobs are what it was asked to change: each `set` and `policy_set` path, a whole
candidate policy (by digest), each policy override (`risk_pct`, the caps, the flat ladder), each
convention (fills, slot order, sizing, start capital) and the run's slippage, cost model and
cooldown. `config.json` records them (`knobs`), so a later variant compares against its base's
record whether the base ran in its sweep or long before. A variant whose knobs differ from its
base's in more than one is a bundle: a sweep refuses it unless it declares `bundle: true` and a
`reason` (`backtest_sweep_not_one_change`), and the run audit warns on it (`one_change`). A
`set` knob is named by the path it resolves to in the version's config (`resolve_knobs`), so
`exits[time_stop].enabled` and `exits[3].enabled` are one knob.

A hold-out run validates one completed in-sample run, its candidate, and runs the candidate's
own config on a later window: the same strategy, policy and conventions (`candidate_hash`, the
resolved config without the window's cost schedules), a window that starts after the
candidate's ends, and no earlier hold-out of the same candidate config.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..confighash import canonical_json, config_hash
from . import folder
from .overrides import OverrideError, policy_digest, set_path
from .spec import VOLUME_PACE_LIVE, BacktestError, RunConventions

__all__ = [
    "HOLD_OUT",
    "candidate_hash",
    "changed_knobs",
    "check_hold_out",
    "knobs_of",
    "resolve_knobs",
]

HOLD_OUT = "hold_out"
# a hold-out that completed or was stopped part way has been seen; a failed one has not
_SEEN = frozenset({folder.COMPLETE, folder.STOPPED})


def _text(value: object) -> str:
    """One text per value: a Decimal by its normalised digits, anything else as canonical JSON."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return canonical_json({"v": value})[5:-1]


def knobs_of(
    conventions: RunConventions,
    slippage_bps: Decimal,
    cost_model: str,
    cooldown_sessions: int | None,
) -> dict[str, str]:
    """A run's knobs by name (`set:<path>`, `policy_set:<path>`, `policy`, a convention's name),
    each value as one text; a path set twice keeps its last value, as the run applies them.
    `volume_pace` and `overlap` are knobs only off their defaults, so a base recorded before
    they existed compares alike."""
    o = conventions.overrides
    out: dict[str, str] = {f"set:{path}": _text(value) for path, value in o.sets}
    out |= {f"policy_set:{path}": _text(value) for path, value in o.policy_sets}
    named: dict[str, object] = {
        "policy": None if o.policy is None else policy_digest(o.policy),
        "risk_pct": o.risk_pct,
        "max_positions": o.max_positions,
        "max_per_industry": o.max_per_industry,
        "flat_ladder": o.flat_ladder,
        "sleeve_weight": conventions.sleeve_weight,
        "slot_order": conventions.slot_order,
        "entry_fill": conventions.entry_fill_rule,
        "ambiguous": conventions.ambiguous,
        "suspension_sessions": conventions.suspension_sessions,
        "regime_mult": conventions.regime_mult,
        "start_capital": conventions.start_capital,
        "entry_bars": conventions.entry_bars,
        "slippage_bps": slippage_bps,
        "cost_model": cost_model,
        "cooldown_sessions": cooldown_sessions,
    }
    if conventions.volume_pace != VOLUME_PACE_LIVE:
        named["volume_pace"] = conventions.volume_pace
    if conventions.overlap:  # likewise only when set
        named["overlap"] = conventions.overlap
    if o.playbooks is not None:  # a knob only when set, so a run before it compares alike
        named["playbooks"] = sorted(o.playbooks)
    return out | {name: _text(value) for name, value in named.items()}


def resolve_knobs(knobs: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, str]:
    """`knobs` with each `set:` path named by the path it resolves to in the strategy config
    `config` (`StrategyConfig.to_json`), the sets applied in their order (`overrides.set_path`),
    so a selector by name and one by index name one knob. A path that names nothing in it is
    kept as written (the run refuses it); a resolved path is its own resolution."""
    doc = copy.deepcopy(dict(config))
    out: dict[str, str] = {}
    for name, value in knobs.items():
        kind, sep, path = name.partition(":")
        if kind == "set" and sep:
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
            try:
                name = f"set:{set_path(doc, path, parsed)}"
            except OverrideError:
                pass
        out[name] = value
    return out


def changed_knobs(mine: Mapping[str, str], base: Mapping[str, str]) -> list[str]:
    """The knobs whose value differs between a variant and its base, sorted; a knob only one of
    them names (a `set` path the other does not) differs."""
    return sorted(k for k in set(mine) | set(base) if mine.get(k) != base.get(k))


def candidate_hash(resolved: Mapping[str, Any]) -> str:
    """sha256 of a run's resolved config (`run.json` `config.resolved`) without its window: the
    cost model keeps its version, not the dated schedules in force in the window. A hold-out
    and the in-sample run it validates hash alike when they ran the same config."""
    conventions = dict(resolved.get("conventions") or {})
    cost = conventions.get("cost_model")
    if isinstance(cost, Mapping):
        conventions["cost_model"] = cost.get("version")
    return config_hash({**resolved, "conventions": conventions})


def check_hold_out(root: Path, validates: str, resolved: Mapping[str, Any], start: date) -> None:
    """Refuse a hold-out run (`BacktestError`) that validates `validates` with the resolved
    config `resolved` from `start`: `backtest_hold_out_unknown_candidate` when the candidate has
    no `run.json`, `backtest_hold_out_bad_candidate` when it is not a completed in-sample run,
    `backtest_hold_out_overlaps` when `start` is not after its last day,
    `backtest_hold_out_config_differs` when the config is not the candidate's, and
    `backtest_hold_out_repeated` when a hold-out of the same candidate config has been seen."""
    candidate = folder.read_run(root, validates)
    if candidate is None:
        raise BacktestError("backtest_hold_out_unknown_candidate")
    data = candidate.get("data") or {}
    if (candidate.get("identity") or {}).get("status") != folder.COMPLETE or data.get(
        "split"
    ) == HOLD_OUT:
        raise BacktestError("backtest_hold_out_bad_candidate")
    end = data.get("period_end")
    if not isinstance(end, str) or start <= date.fromisoformat(end):
        raise BacktestError("backtest_hold_out_overlaps")
    mine = candidate_hash(resolved)
    if candidate_hash((candidate.get("config") or {}).get("resolved") or {}) != mine:
        raise BacktestError("backtest_hold_out_config_differs")
    for doc in folder.list_runs(root):
        seen = (doc.get("identity") or {}).get("status") in _SEEN
        if (
            seen
            and (doc.get("data") or {}).get("split") == HOLD_OUT
            and candidate_hash((doc.get("config") or {}).get("resolved") or {}) == mine
        ):
            raise BacktestError("backtest_hold_out_repeated")
