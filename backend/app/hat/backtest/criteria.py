"""A strategy's promotion criteria as data, and their evaluation against run folders
(docs/backtests/process.md, rule 1 and step 6).

The criteria live in the strategy's notes as one fenced block tagged `hat-criteria`, in YAML:

    ```hat-criteria
    strategy: S1
    universe: NIFTY500_PIT
    in_sample: {from: 2016-01-01, to: 2021-12-31}
    hold_out: {from: 2022-01-01, to: 2026-10-23}
    trial_budget: 30
    checks: {mar_min: 0.6, longest_dd_sessions_below: 500, trades_min: 100, ...}
    plateau: {mar_min: 0.5, parameters: [{path: strategy.exits.4.lookback_days, step: 5}]}
    hold_out_checks: {expectancy_min_share: 0.5, mar_min: 0.4}
    ```

`digest` is the sha256 of the block's text, so a review records exactly which criteria it
applied and a later change is visible. `evaluate` computes every criterion from the run folders
alone and never decides by hand: a criterion it cannot compute (no plateau sweep, no hold-out
run yet) is `not_run`, and the candidate passes only when every row passes.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml

from .standard import changed_paths

__all__ = [
    "FENCE",
    "Criteria",
    "Parameter",
    "Row",
    "extract",
    "hold_out_rows",
    "in_sample_rows",
    "overall",
    "parse",
    "plateau_rows",
    "trial_rows",
]

FENCE = "hat-criteria"
PASS, FAIL, NOT_RUN = "pass", "fail", "not_run"
_BLOCK = re.compile(rf"^```{FENCE}[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
# the in-sample checks a block may set (`in_sample_rows` reads each)
CHECKS = (
    "mar_min",
    "longest_dd_sessions_below",
    "trades_min",
    "expectancy_r_min",
    "cagr_above_benchmark",
    "cagr_ex_top5_min_share",
    "max_pct_adv",
    "binding_required",
    "dsr_prob_min",
    "pbo_max",
)
HOLD_OUT_CHECKS = ("expectancy_min_share", "mar_min")
_KEYS = {
    "strategy",
    "universe",
    "in_sample",
    "hold_out",
    "trial_budget",
    "checks",
    "plateau",
    "hold_out_checks",
}


@dataclass(frozen=True)
class Parameter:
    """A key parameter of the plateau: a dotted path into a run's `config.resolved` (as
    `config.overrides` names it) and its step; a boolean's one neighbour is its negation."""

    path: str
    step: float | None


@dataclass(frozen=True)
class Criteria:
    strategy: str
    universe: str
    in_sample: tuple[str, str]
    hold_out: tuple[str, str] | None
    trial_budget: int
    checks: Mapping[str, float | bool]
    plateau_mar_min: float | None
    parameters: tuple[Parameter, ...]
    hold_out_checks: Mapping[str, float]
    text: str
    digest: str


@dataclass(frozen=True)
class Row:
    section: str  # in_sample | trials | plateau | hold_out
    name: str
    value: object
    threshold: str
    status: str  # pass | fail | not_run
    note: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


def extract(notes: str) -> str:
    """The one `hat-criteria` block of a notes file; `criteria_block_missing` or
    `criteria_block_many` (ValueError) otherwise."""
    found = _BLOCK.findall(notes.replace("\r\n", "\n"))
    if not found:
        raise ValueError("criteria_block_missing")
    if len(found) > 1:
        raise ValueError("criteria_block_many")
    return found[0]


def _bad(key: str) -> ValueError:
    return ValueError(f"criteria_bad_{key}")


def _number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _bad(key)
    return float(value)


def _window(value: object, key: str) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"from", "to"}:
        raise _bad(key)
    start, end = value["from"], value["to"]
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise _bad(key)
    return start.isoformat(), end.isoformat()


def _checks(value: object, known: Sequence[str], key: str) -> dict[str, float | bool]:
    if not isinstance(value, Mapping) or not set(value) <= set(known):
        raise _bad(key)
    out: dict[str, float | bool] = {}
    for name, item in value.items():
        if name in {"cagr_above_benchmark", "binding_required"}:
            if not isinstance(item, bool):
                raise _bad(name)
            out[name] = item
        else:
            out[name] = _number(item, name)
    return out


def _parameters(value: object) -> tuple[Parameter, ...]:
    if not isinstance(value, list):
        raise _bad("plateau")
    out = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise _bad("plateau")
        if not set(item) <= {"path", "step"}:
            raise _bad("plateau")
        step = item.get("step")
        out.append(Parameter(item["path"], None if step is None else _number(step, "plateau")))
    return tuple(out)


def parse(text: str) -> Criteria:
    """The criteria of a block's text; `criteria_bad_<key>` (ValueError) for a block that is
    not one (an unknown key, a missing field, a value of the wrong kind)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _bad("yaml") from exc
    if not isinstance(raw, Mapping) or not set(raw) <= _KEYS:
        raise _bad("keys")
    for key in ("strategy", "universe"):
        if not isinstance(raw.get(key), str):
            raise _bad(key)
    budget = raw.get("trial_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise _bad("trial_budget")
    plateau = raw.get("plateau") or {}
    if not isinstance(plateau, Mapping) or not set(plateau) <= {"mar_min", "parameters"}:
        raise _bad("plateau")
    mar_min = plateau.get("mar_min")
    return Criteria(
        strategy=raw["strategy"],
        universe=raw["universe"],
        in_sample=_window(raw.get("in_sample"), "in_sample"),
        hold_out=None if raw.get("hold_out") is None else _window(raw["hold_out"], "hold_out"),
        trial_budget=budget,
        checks=_checks(raw.get("checks") or {}, CHECKS, "checks"),
        plateau_mar_min=None if mar_min is None else _number(mar_min, "plateau"),
        parameters=_parameters(plateau.get("parameters") or []),
        hold_out_checks={
            k: float(v)
            for k, v in _checks(
                raw.get("hold_out_checks") or {}, HOLD_OUT_CHECKS, "hold_out_checks"
            ).items()
        },
        text=text,
        digest=hashlib.sha256(text.encode()).hexdigest(),
    )


# --- evaluation ------------------------------------------------------------------------------


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _status(ok: bool | None) -> str:
    return FAIL if not ok else PASS


def _metrics(run: Mapping[str, Any]) -> Mapping[str, Any]:
    return run.get("metrics") or {}


def _window_of(run: Mapping[str, Any]) -> tuple[object, object]:
    data = run.get("data") or {}
    return data.get("period_start"), data.get("period_end")


def _scope(run: Mapping[str, Any], c: Criteria, window: tuple[str, str], section: str) -> Row:
    data = run.get("data") or {}
    status = (run.get("identity") or {}).get("status")
    got = f"{data.get('universe')} {data.get('period_start')}..{data.get('period_end')}"
    ok = data.get("universe") == c.universe and _window_of(run) == window and status == "complete"
    return Row(
        section,
        "scope",
        got if status == "complete" else f"{got} ({status})",
        f"complete, {c.universe} {window[0]}..{window[1]}",
        _status(ok),
    )


def _at_least(section: str, name: str, value: object, floor: float) -> Row:
    number = _num(value)
    return Row(
        section, name, number, f">= {floor:g}", _status(number is not None and number >= floor)
    )


def max_pct_adv(trades: Sequence[Mapping[str, str]]) -> tuple[float | None, list[float]]:
    """(the largest entry's % of 20-day traded value, every entry's) from `trades.csv` rows (a
    trade's first leg; the entry is the same on every leg)."""
    values = []
    for row in trades:
        if row.get("leg") not in {None, "1"}:
            continue
        try:
            values.append(float(row.get("position_pct_adv") or "nan"))
        except ValueError:
            continue
    values = [v for v in values if math.isfinite(v)]
    return (max(values) if values else None), values


def in_sample_rows(
    c: Criteria, run: Mapping[str, Any], trades: Sequence[Mapping[str, str]]
) -> list[Row]:
    """The in-sample criteria of `run` (its `run.json`, its `trades.csv` rows)."""
    m, s = _metrics(run), "in_sample"
    rows = [_scope(run, c, c.in_sample, s)]
    k = c.checks
    if "mar_min" in k:
        rows.append(_at_least(s, "mar", m.get("mar"), float(k["mar_min"])))
    if "longest_dd_sessions_below" in k:
        bound, dd = float(k["longest_dd_sessions_below"]), _num(m.get("longest_dd_sessions"))
        rows.append(
            Row(
                s, "longest_dd_sessions", dd, f"< {bound:g}", _status(dd is not None and dd < bound)
            )
        )
    if "trades_min" in k:
        rows.append(_at_least(s, "trades", m.get("trades"), float(k["trades_min"])))
    if "expectancy_r_min" in k:
        rows.append(
            _at_least(s, "expectancy_r", m.get("expectancy_r"), float(k["expectancy_r_min"]))
        )
    if k.get("cagr_above_benchmark"):
        cagr, bench = _num(m.get("cagr_pct")), _num(m.get("benchmark_cagr_pct"))
        ok = cagr is not None and bench is not None and cagr > bench
        rows.append(
            Row(s, "cagr_pct vs benchmark", cagr, f"> {bench} (NIFTY 500 TRI)", _status(ok))
        )
    if "cagr_ex_top5_min_share" in k:
        share = float(k["cagr_ex_top5_min_share"])
        cagr, ex = _num(m.get("cagr_pct")), _num(m.get("cagr_ex_top5_trades_pct"))
        floor = None if cagr is None else share * cagr
        ok = ex is not None and floor is not None and cagr is not None and cagr > 0 and ex >= floor
        shown = "-" if floor is None else f"{floor:.4g}"
        rows.append(
            Row(s, "cagr_ex_top5_trades_pct", ex, f">= {share:g} x cagr_pct = {shown}", _status(ok))
        )
    if "max_pct_adv" in k:
        limit = float(k["max_pct_adv"])
        top, values = max_pct_adv(trades)
        over = sum(1 for v in values if v > limit)
        unknown = (_num(m.get("trades")) or 0) > 0 and not values
        note = f"{over} entries above" if over else None
        if unknown:
            note = "no ADV on the entries"
        rows.append(
            Row(
                s,
                "max position % ADV",
                top,
                f"<= {limit:g} on every entry",
                _status(over == 0 and not unknown),
                note,
            )
        )
    if k.get("binding_required"):
        no_op = (run.get("binding") or {}).get("no_op")
        note = "no base: binding unknown" if no_op is None else None
        rows.append(Row(s, "binding.no_op", no_op, "false", _status(no_op is False), note))
    if "dsr_prob_min" in k:
        rows.append(_at_least(s, "dsr_prob", m.get("dsr_prob"), float(k["dsr_prob_min"])))
    if "pbo_max" in k:
        pbo, cap = _num(m.get("pbo")), float(k["pbo_max"])
        rows.append(Row(s, "pbo", pbo, f"<= {cap:g}", _status(pbo is not None and pbo <= cap)))
    return rows


def trial_rows(
    c: Criteria, run: Mapping[str, Any], family: str, total: int, prior: int
) -> list[Row]:
    """The trial budget: the family's own trials (the registry's, less the prior ones seeded
    from deleted runs) within `trial_budget`; the DSR's counts beside it."""
    m = _metrics(run)
    own = total - prior
    return [
        Row(
            "trials",
            "trial budget",
            own,
            f"<= {c.trial_budget}",
            _status(own <= c.trial_budget),
            f"{total} trials in {family}, {prior} of them prior (seeded); the run's DSR counted"
            f" {m.get('dsr_n_trials')} (scored at {m.get('dsr_n_trials_effective', '-')})",
            {"family": family, "trials": total, "prior": prior, "own": own},
        )
    ]


def _leaf(resolved: Mapping[str, Any], path: str) -> object:
    flat = changed_paths(resolved, {})
    return flat.get(path, _MISSING)


_MISSING = object()


def _side(value: object, mine: object, step: float | None) -> str | None:
    """Which neighbour `value` is of the candidate's `mine`: `-`, `+`, `flip`, or None."""
    if isinstance(mine, bool):
        return "flip" if value is (not mine) else None
    a, b = _num(mine), _num(value)
    if a is None or b is None or step is None:
        return None
    if math.isclose(b, a - step, abs_tol=1e-9):
        return "-"
    return "+" if math.isclose(b, a + step, abs_tol=1e-9) else None


def plateau_rows(
    c: Criteria, run: Mapping[str, Any], sweep: str | None, neighbours: Sequence[Mapping[str, Any]]
) -> list[Row]:
    """Each key parameter one step either side (a boolean: flipped) of `run`: the completed runs
    of the plateau sweep on the run's universe and window that differ from it in that one
    resolved path by that step must each keep MAR >= the plateau's floor. A side with no run is
    `not_run`; no parameters or no sweep is one `not_run` row."""
    s = "plateau"
    floor = c.plateau_mar_min
    if not c.parameters or floor is None:
        return [Row(s, "plateau", None, "key parameters and a MAR floor", NOT_RUN, "not defined")]
    if sweep is None:
        return [
            Row(s, "plateau", None, f"MAR >= {floor:g} one step either side", NOT_RUN, "no sweep")
        ]
    resolved = (run.get("config") or {}).get("resolved") or {}
    found: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for other in neighbours:
        if (
            (other.get("identity") or {}).get("status") != "complete"
            or (other.get("data") or {}).get("universe") != (run.get("data") or {}).get("universe")
            or _window_of(other) != _window_of(run)
        ):
            continue
        diff = changed_paths((other.get("config") or {}).get("resolved") or {}, resolved)
        if len(diff) != 1:
            continue
        ((path, value),) = diff.items()
        for p in c.parameters:
            side = None if p.path != path else _side(value, _leaf(resolved, path), p.step)
            if side is not None:
                found.setdefault((path, side), []).append(other)
    rows = []
    for p in c.parameters:
        mine = _leaf(resolved, p.path)
        if mine is _MISSING:
            rows.append(Row(s, p.path, None, "a path of the run", FAIL, "not in config.resolved"))
            continue
        for side in ("flip",) if isinstance(mine, bool) else ("-", "+"):
            runs = found.get((p.path, side), [])
            mars = [_num(_metrics(r).get("mar")) for r in runs]
            ids = [str((r.get("identity") or {}).get("run_id"))[:8] for r in runs]
            status = (
                NOT_RUN if not runs else _status(all(v is not None and v >= floor for v in mars))
            )
            label = f"{p.path} {side}" if side == "flip" else f"{p.path} {side}{p.step:g}"
            rows.append(
                Row(
                    s,
                    label,
                    mars[0] if len(mars) == 1 else mars or None,
                    f"MAR >= {floor:g}",
                    status,
                    ", ".join(ids) or "no run",
                )
            )
    return rows


def hold_out_rows(
    c: Criteria,
    in_sample: Mapping[str, Any],
    hold_out: Mapping[str, Any] | None,
    reads: int,
) -> list[Row]:
    """The hold-out criteria: the hold-out run validating `in_sample` (None: not run yet) on the
    criteria's hold-out window, read once (`reads`: the hold-out runs that validate it), its
    expectancy at least the share of the in-sample one, its MAR at least the floor."""
    s = "hold_out"
    if c.hold_out is None and not c.hold_out_checks:
        return []
    if hold_out is None:
        return [Row(s, "hold-out run", None, "one run validating this run", NOT_RUN, "not run yet")]
    rows = []
    if c.hold_out is not None:
        rows.append(_scope(hold_out, c, c.hold_out, s))
    rows.append(Row(s, "hold-out reads", reads, "1", _status(reads == 1)))
    m, k = _metrics(hold_out), c.hold_out_checks
    if "expectancy_min_share" in k:
        share = k["expectancy_min_share"]
        mine, theirs = _num(m.get("expectancy_r")), _num(_metrics(in_sample).get("expectancy_r"))
        floor = None if theirs is None else share * theirs
        ok = mine is not None and floor is not None and theirs is not None and theirs > 0
        ok = ok and mine >= floor
        shown = "-" if floor is None else f"{floor:.4g}"
        rows.append(
            Row(
                s, "expectancy_r", mine, f">= {share:g} x in-sample {theirs} = {shown}", _status(ok)
            )
        )
    if "mar_min" in k:
        rows.append(_at_least(s, "mar", m.get("mar"), k["mar_min"]))
    return rows


def overall(rows: Sequence[Row]) -> str:
    """`fail` when a row fails, `incomplete` when none fails and one is not run, else `pass`."""
    statuses = {r.status for r in rows}
    if FAIL in statuses:
        return FAIL
    return "incomplete" if NOT_RUN in statuses else PASS
