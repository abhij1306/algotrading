"""The S1 setups diagnosis (owner 2026-10-30): the sweeps of the five setups and the
leave-one-out funnel. A diagnostic: it writes variants files and reads run folders, and never
changes a normal run. In-sample 2016-01-01..2021-12-31 only.

    venv/bin/python scripts/research/s1_setups_diag.py sweeps --out DIR
    HAT_DATA_ROOT=<data> venv/bin/python scripts/research/s1_setups_diag.py loo \\
        --log backtests/s1-diag-loo.log

`sweeps` writes three variants files (each run with `python -m app.hat.backtest --version <v>
--universe <u> --from 2016-01-01 --to 2021-12-31 --variants FILE`):

- `s1-diag-setups.yaml` (`--universe nifty500`): r1 VCP (`book_vcp`), r2a-d each of setups 2-5
  alone, r3 all five on one account (one setup per instrument), each on s1-book-01's exits,
  sizing and policy overrides (`BASE`, `EXITS`, `ACCOUNT`);
- `s1-diag-universe.yaml` (`--universe NSE_STOCKS`): r3 on every NSE stock (DVRs included, no
  ETF or fund) of series EQ only (no BE: no REIT, InvIT, SME or debt series either) with the
  turnover floor at 5 crore and no price floor;
- `s1-diag-loo.yaml` (`--universe nifty500`): `shadow`, r3 without cash or slot limits
  (`SHADOW`: 0.02% risk on 100 crore, a position cap and day caps no run reaches, no ADV cap,
  no minimum position), then one variant per rule of `RULES` with only that rule removed, from
  every playbook that has it (each playbook scans on its own, so a rule shared by several is
  removed from each at once and the extra entries are split by setup tag). Every entry is
  traded with the normal stop and exits: a shadow trade.

`loo` reads the runs a leave-one-out sweep logged (`--log`, the sweep's printed lines) and
prints, per rule: the extra setups (the funnel's `setup` count over the shadow run's), the extra
entries (entries, by instrument and entry date, the shadow run has not) and the entries lost,
and the extra entries' count, win % and expectancy R (a trade's legs summed), overall and per
setup tag, beside the shadow run's own. One setup per instrument means a removed rule can swap
the setup an instrument takes, at another pivot and so often on another date: a lost entry
whose instrument has an extra entry is counted as a swap.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

BASE = "01a18c82-cd7b-7785-8a27-0cce7410c850"  # s1-book-01 rerun (book exits, sizing, regime)
TT = [  # the trend template's eight price checks (RS rank added per playbook)
    {"type": "sma_above", "params": {"a": "close", "b": 150}},
    {"type": "sma_above", "params": {"a": "close", "b": 200}},
    {"type": "sma_above", "params": {"a": 150, "b": 200}},
    {"type": "sma_rising", "params": {"sma": 200, "lookback_days": 21}},
    {"type": "sma_above", "params": {"a": 50, "b": 150}},
    {"type": "sma_above", "params": {"a": 50, "b": 200}},
    {"type": "sma_above", "params": {"a": "close", "b": 50}},
    {"type": "pct_from_52w_low", "params": {"min_pct": 30}},
    {"type": "pct_from_52w_high", "params": {"max_pct_below": 25}},
]
ONE_SETUP = "one_setup_per_instrument=true"
STAGE_GATES = frozenset({"sma_above", "sma_rising", "pct_from_52w_low", "pct_from_52w_high"})


def _rs(n: int) -> dict[str, Any]:
    return {"type": "rs_rank", "params": {"min_percentile": n, "basis": "eligible"}}


GATES: dict[str, list[dict[str, Any]]] = {  # each playbook's stage gate, then its detector
    "vcp": [*TT, _rs(70), {"type": "book_vcp"}],
    "cheat": [
        {"type": "sma_above", "params": {"a": "close", "b": 50}},
        {"type": "sma_rising", "params": {"sma": 50, "lookback_days": 5}},
        _rs(70),
        {"type": "book_cheat"},
    ],
    "cup_handle": [*TT, _rs(70), {"type": "book_cup_handle"}],
    "power_play": [
        {"type": "sma_above", "params": {"a": "close", "b": 50}},
        _rs(80),
        {"type": "book_power_play"},
    ],
    "primary_base": [{"type": "book_primary_base"}],  # its own history-dependent gates
}
SETUPS = tuple(GATES)
# the playbook each setup tag comes from
TAGS = {
    "vcp": "vcp",
    "cheat_3c": "cheat",
    "low_cheat": "cheat",
    "cup_handle": "cup_handle",
    "power_play": "power_play",
    "primary_base": "primary_base",
}
# s1-book-01's: close not above the pivot at the scan; then the shared criteria
AFTER = [
    {"type": "close_vs_pivot", "params": {"max_above_pct": 0.0}},
    {"type": "stop_cap"},
    {"type": "overhead_supply"},
    {"type": "setup_tags"},
]


def playbook(name: str) -> dict[str, Any]:
    criteria = [
        {"stage": "setup", "mode": "hard", "order": n, "params": {}, **copy.deepcopy(c)}
        for n, c in enumerate([*GATES[name], *AFTER])
    ]
    return {
        "name": name,
        "valid_sessions": 5,
        "setup": {"threshold": 0.6, "criteria": criteria},
        "trigger": [  # s1-book-01's: the pivot's break, volume pace >= 1.5 x ADV50
            {
                "type": "price_breaks_level",
                "stage": "trigger",
                "params": {"level": "pivot", "bar": "5m", "confirm": "close"},
            },
            {"type": "volume_pace", "stage": "trigger", "params": {"vs_avg50": 1.5}},
        ],
        "entry_filters": [{"type": "max_chase", "stage": "entry_filter", "params": {"pct": 2.0}}],
        "stop": {
            "rule": "below_final_contraction_low",
            "buffer_pct": 0.5,
            "max_pct": 10.0,
            "min_pct": 0.0,
        },
    }


EXITS = [  # s1-book-01's stack, repeated (a variant does not inherit its base's sets)
    'exits[+]={"rule": "move_stop_to_breakeven", "at_r": 2.5}',
    "exits[partial_at_r].enabled=true",
    "exits[partial_at_r].at_r=3",
    "exits[partial_at_r].sell_fraction=0.5",
    "exits[close_below_sma].sma=20",
    "exits[close_below_sma].grace_sessions=10",
    "exits[trailing_swing_low].enabled=false",
    "exits[time_stop].enabled=false",
    "exits[step_trail].enabled=false",
]
POLICY = ["global_open_risk_pct=1", "p_max.stage2-contraction-breakout=0.25", "cooldown_sessions=0"]
ACCOUNT: dict[str, Any] = {  # s1-book-01's sizing and policy v3 overrides
    "risk_pct": 0.01,
    "max_positions": 6,
    "slot_order": "leader_rs",
    "flat_ladder": True,
    "ambiguous": "pessimistic",
    "policy_set": POLICY,
}
UNLIMITED = 1000  # more positions or entries than any night has setups
SHADOW: dict[str, Any] = {  # no cash or slot limit: every entry is taken
    **ACCOUNT,
    "risk_pct": 0.0002,
    "max_positions": UNLIMITED,
    "start_capital": 1_000_000_000,
    "policy_set": [
        *POLICY,
        "adv_cap_pct=1",
        "min_position_pct=0",
        f"max_open_positions={UNLIMITED}",
        f"max_positions_per_industry={UNLIMITED}",
        f"max_entries_per_day={UNLIMITED}",
        f"max_stopouts_per_day={UNLIMITED}",
        *(f"exposure_ladder.{lv}.max_positions={UNLIMITED}" for lv in ("L1", "L2", "L3")),
    ],
}
REASON = "the plan's setup playbooks as one candidate (owner 2026-10-30); no part is tuned"
LOO_REASON = "leave-one-out diagnostic (owner 2026-10-30): not a candidate"
REGIME_OFF = 'regime={"index": "NIFTY 500", "rule": "always_on", "params": {}}'


def books_set(books: Sequence[dict[str, Any]]) -> str:
    return "playbooks=" + json.dumps(list(books))


# --- removing one rule ------------------------------------------------------------------------

Books = list[dict[str, Any]]
DETECTORS = frozenset(f"book_{name}" for name in SETUPS)


def _drop(*types: str) -> Callable[[Books], Books]:
    """Remove every setup criterion of `types` from each playbook."""

    def apply(books: Books) -> Books:
        for book in books:
            crit = book["setup"]["criteria"]
            book["setup"]["criteria"] = [c for c in crit if c["type"] not in types]
        return books

    return apply


def _params(values: Mapping[str, Mapping[str, Any]]) -> Callable[[Books], Books]:
    """Set detector params: `values` maps a detector type to the params it takes."""

    def apply(books: Books) -> Books:
        for book in books:
            for c in book["setup"]["criteria"]:
                if c["type"] in values:
                    c["params"] = {**c["params"], **values[c["type"]]}
        return books

    return apply


def _then(*steps: Callable[[Books], Books]) -> Callable[[Books], Books]:
    def apply(books: Books) -> Books:
        for step in steps:
            books = step(books)
        return books

    return apply


def _no_volume(books: Books) -> Books:
    for book in books:
        book["trigger"] = [t for t in book["trigger"] if t["type"] != "volume_pace"]
    return books


def _no_chase(books: Books) -> Books:
    for book in books:
        for f in book["entry_filters"]:
            if f["type"] == "max_chase":
                f["params"] = {**f["params"], "pct": 100.0}  # process.md: a very wide chase is off
    return books


DRY_OFF = {"dryup_mean_max": None, "dryup_session_max": None}


@dataclass(frozen=True)
class Rule:
    name: str
    removes: str  # what is removed, for the report
    books: Callable[[Books], Books] | None = None  # the playbooks' change
    sets: tuple[str, ...] = ()  # or a config path


RULES: tuple[Rule, ...] = (
    Rule(
        "trend_template",
        "the stage gate (TT price checks; cheat/power play their relaxed gates; primary base "
        "its SMA50 gate)",
        _then(_drop(*STAGE_GATES), _params({"book_primary_base": {"sma50_min_history": None}})),
    ),
    Rule(
        "rs_rank",
        "RS rank (>= 70; power play >= 80; primary base's own)",
        _then(_drop("rs_rank"), _params({"book_primary_base": {"rs_min_history": None}})),
    ),
    Rule(
        "base_length",
        "base / cup / flag length",
        _params(
            {
                "book_vcp": {"base_sessions": [2, 325]},
                "book_cheat": {"cup_sessions": [2, 325]},
                "book_cup_handle": {"cup_sessions": [2, 325]},
                "book_power_play": {"flag_sessions": [2, 60]},
                "book_primary_base": {"base_min_sessions": 2},
            }
        ),
    ),
    Rule(
        "base_depth",
        "first-contraction band / cup depth / flag depth / base depth",
        _params(
            {
                "book_vcp": {"first_depth_pct": [0.0, 100.0]},
                "book_cheat": {"cup_depth_pct": [0.01, 100.0]},
                "book_cup_handle": {"cup_depth_pct": [0.01, 100.0]},
                "book_power_play": {"flag_depth_max_pct": 100.0},
                "book_primary_base": {"base_depth_max_pct": 100.0},
            }
        ),
    ),
    Rule(
        "contraction_count",
        "contraction count",
        _params(
            {
                "book_vcp": {"contractions": [1, 12]},
                "book_power_play": {"min_contractions": 1},
                "book_primary_base": {"contractions": [1, 12]},
            }
        ),
    ),
    Rule(
        "depth_ratio",
        "each contraction <= 0.75 x the one before (the tight pullback's too)",
        _params(
            {
                "book_vcp": {"depth_ratio_max": 1.0},
                "book_power_play": {"depth_ratio_max": 1.0},
                "book_primary_base": {"depth_ratio_max": 1.0},
            }
        ),
    ),
    Rule(
        "final_depth",
        "final contraction / pause depth cap",
        _params(
            {
                "book_vcp": {"final_depth_max_pct": 100.0},
                "book_cheat": {"pause_range_max_pct": 100.0},
                "book_power_play": {"final_depth_max_pct": 100.0},
                "book_primary_base": {"final_depth_max_pct": 100.0},
            }
        ),
    ),
    Rule("dry_up", "volume dry-up", _params({d: dict(DRY_OFF) for d in DETECTORS})),
    Rule(
        "time_compression",
        "time compression (VCP)",
        _params({"book_vcp": {"tc_min_ratio": None, "tc_min_sessions": None}}),
    ),
    Rule(
        "relative_correction",
        "relative correction (VCP)",
        _params({"book_vcp": {"rc_max_ratio": None}}),
    ),
    Rule("base_count", "base count (VCP)", _params({"book_vcp": {"max_base_number": None}})),
    Rule("overhead_supply", "overhead supply", _drop("overhead_supply")),
    Rule("regime", "the regime filter (always on)", sets=(REGIME_OFF,)),
    Rule("breakout_volume", "breakout volume (pace >= 1.5 x ADV50)", _no_volume),
    Rule("max_chase", "2% chase", _no_chase),
    Rule(
        "low_before_pause",
        "cheat: C at least 10 sessions before the pause",
        _params({"book_cheat": {"low_before_pause_sessions": 0}}),
    ),
    Rule(
        "h0_template",
        "cheat: template passed on H0's date",
        _params({"book_cheat": {"h0_template": False}}),
    ),
    Rule(
        "handle_length",
        "handle 5-25 sessions",
        _params({"book_cup_handle": {"handle_sessions": [1, 325]}}),
    ),
    Rule(
        "handle_position",
        "handle low in the cup's upper third",
        _params({"book_cup_handle": {"handle_low_min_fraction": 0.0}}),
    ),
    Rule(
        "handle_depth",
        "handle depth <= 12%",
        _params({"book_cup_handle": {"handle_depth_max_pct": 100.0}}),
    ),
)


def rule_sets(rule: Rule, names: Sequence[str] = SETUPS) -> list[str]:
    """The `set` paths of the shadow run with only `rule` removed."""
    books = [playbook(n) for n in names]
    if rule.books is not None:
        books = rule.books(books)
    return [books_set(books), ONE_SETUP, *rule.sets, *EXITS]


# --- the sweeps -------------------------------------------------------------------------------


def _variant(label: str, change: str, sets: list[str], account: Mapping[str, Any]) -> dict:
    return {
        "label": label,
        "base": BASE,
        "change": change,
        "bundle": True,
        "reason": REASON,
        "set": [*sets, *EXITS],
        **account,
    }


def setups_sweep() -> dict[str, Any]:
    alone = [
        _variant(label, f"setup {name} alone", [books_set([playbook(name)])], ACCOUNT)
        for label, name in zip(("r1", "r2a", "r2b", "r2c", "r2d"), SETUPS, strict=True)
    ]
    combined = _variant(
        "r3",
        "setups 1-5 together, one setup per instrument, leader_rs",
        [books_set([playbook(n) for n in SETUPS]), ONE_SETUP],
        ACCOUNT,
    )
    return {
        "sweep_id": "s1-diag-setups",
        "sweep_name": "S1 diagnosis: setups 1-5 alone and together (window fix)",
        "variants": [*alone, combined],
    }


def universe_sweep() -> dict[str, Any]:
    combined = _variant(
        "r3-nse",
        "setups 1-5 together on every NSE stock: series EQ, turnover >= 5 crore, no price floor",
        [
            books_set([playbook(n) for n in SETUPS]),
            ONE_SETUP,
            'universe[series_in].params.series=["EQ"]',
            "universe[min_turnover].params.inr_cr=5",
            "universe[min_price].enabled=false",
        ],
        ACCOUNT,
    )
    return {
        "sweep_id": "s1-diag-universe",
        "sweep_name": "S1 diagnosis: setups 1-5 on NSE stocks with turnover >= 5 crore",
        "variants": [combined],
    }


def loo_sweep(rules: Iterable[Rule] = RULES) -> dict[str, Any]:
    shadow = {
        "label": "shadow",
        "base": BASE,
        "change": "setups 1-5 together, no cash or slot limits (shadow trades)",
        "bundle": True,
        "reason": LOO_REASON,
        "set": rule_sets(Rule("none", "nothing")),
        **SHADOW,
    }
    variants = [
        {"label": r.name, "base": "shadow", "change": f"without {r.removes}", "set": rule_sets(r)}
        | SHADOW
        for r in rules
    ]
    return {
        "sweep_id": "s1-diag-loo",
        "sweep_name": "S1 diagnosis: leave-one-out funnel (shadow trades)",
        "variants": [shadow, *variants],
    }


def inside_cwd(path: Path) -> Path:
    """`path` resolved, refused unless it stays inside the working directory."""
    base = Path.cwd().resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise SystemExit(f"{path} is outside the working directory")
    return target


def write_sweeps(out: Path) -> list[Path]:
    out = inside_cwd(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for doc in (setups_sweep(), universe_sweep(), loo_sweep()):
        path = out / f"{doc['sweep_id']}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, width=1000))
        paths.append(path)
    return paths


# --- the leave-one-out report -----------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    key: tuple[str, str]  # (instrument, entry date)
    setup: str
    r: Decimal


def trades_of(rows: Iterable[Mapping[str, str]]) -> list[Trade]:
    """One trade per `trade_id` of a run's `trades.csv` rows: its first leg's instrument, entry
    date and setup tag, its legs' `r_net` summed."""
    legs: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        legs[row["trade_id"]].append(row)
    out = []
    for parts in legs.values():
        first = parts[0]
        r = sum((Decimal(p["r_net"]) for p in parts), Decimal(0))
        setup = first.get("setup_type") or first.get("playbook") or ""
        out.append(Trade((first["instrument_id"], first["entry_date"]), setup, r))
    return out


@dataclass(frozen=True)
class Stats:
    count: int
    win_pct: float | None
    expectancy_r: float | None


def stats(trades: Sequence[Trade]) -> Stats:
    """Count, win % (R above 0) and mean R; None without a trade."""
    if not trades:
        return Stats(0, None, None)
    wins = sum(1 for t in trades if t.r > 0)
    mean = sum((t.r for t in trades), Decimal(0)) / len(trades)
    return Stats(len(trades), 100.0 * wins / len(trades), float(mean))


def entry_diff(base: Sequence[Trade], variant: Sequence[Trade]) -> tuple[list[Trade], list[Trade]]:
    """(the variant's entries the base has not, the base's entries the variant has not), by
    instrument and entry date."""
    have = {t.key for t in base}
    got = {t.key for t in variant}
    return [t for t in variant if t.key not in have], [t for t in base if t.key not in got]


def swaps(added: Sequence[Trade], lost: Sequence[Trade]) -> int:
    """The lost entries whose instrument has an extra entry: one setup per instrument swapped
    the setup it took."""
    instruments = {t.key[0] for t in added}
    return sum(1 for t in lost if t.key[0] in instruments)


def extra_setups(base: Mapping[str, Any], variant: Mapping[str, Any]) -> dict[str, int]:
    """Per playbook, the funnel's `setup` stock-days of `variant` over `base` (`run.json`)."""
    a = base.get("by_playbook", {})
    b = variant.get("by_playbook", {})
    return {p: int(b[p].get("setup", 0)) - int(a.get(p, {}).get("setup", 0)) for p in b}


def logged_runs(lines: Iterable[str]) -> dict[str, str]:
    """label -> run id of a sweep's printed JSON lines with a run id and status `complete`."""
    out = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        doc = json.loads(line)
        if doc.get("run_id") and doc.get("status") == "complete":
            out[str(doc["label"])] = str(doc["run_id"])
    return out


def _fmt(value: float | None, digits: int) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _cells(s: Stats) -> str:
    return f"{s.count} | {_fmt(s.win_pct, 1)} | {_fmt(s.expectancy_r, 3)}"


def _read_run(root: Path, run_id: str) -> tuple[list[Trade], dict[str, Any]]:
    folder = root / run_id
    with (folder / "trades.csv").open(newline="") as fh:
        trades = trades_of(csv.DictReader(fh))
    run = json.loads((folder / "run.json").read_text())
    return trades, run.get("funnel", {})


def loo_report(runs: Mapping[str, str], root: Path) -> str:
    base_trades, base_funnel = _read_run(root, runs["shadow"])
    lines = [
        f"Shadow run `{runs['shadow']}`: regime-off nights {base_funnel.get('regime_off_nights')}",
        "",
        "| setup | shadow trades | win % | expectancy R |",
        "|---|---|---|---|",
    ]
    by_setup: dict[str, list[Trade]] = defaultdict(list)
    for t in base_trades:
        by_setup[t.setup].append(t)
    for setup in sorted(by_setup):
        lines.append(f"| {setup} | {_cells(stats(by_setup[setup]))} |")
    lines.append(f"| all | {_cells(stats(base_trades))} |")
    head = [
        "",
        "One setup per instrument: removing a rule can swap the setup an instrument takes (at "
        "another pivot, often on another date), so some lost entries are swaps; `lost (swaps)` "
        "counts the lost entries whose instrument has an extra entry.",
        "",
        "| rule | run | extra setups | extra entries | lost (swaps) | shadow win % | "
        "shadow expectancy R | by setup (n, win %, exp R) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += head
    for rule in RULES:
        run_id = runs.get(rule.name)
        if run_id is None:
            lines.append(f"| {rule.name} | not run | | | | | | |")
            continue
        trades, funnel = _read_run(root, run_id)
        added, lost = entry_diff(base_trades, trades)
        setups = extra_setups(base_funnel, funnel)
        total = stats(added)
        parts: dict[str, list[Trade]] = defaultdict(list)
        for t in added:
            parts[t.setup].append(t)
        split = "; ".join(
            f"{k} {len(v)}, {_fmt(stats(v).win_pct, 0)}, {_fmt(stats(v).expectancy_r, 2)}"
            for k, v in sorted(parts.items())
        )
        lines.append(
            f"| {rule.name} | `{run_id}` | {sum(setups.values())} | {total.count} "
            f"| {len(lost)} ({swaps(added, lost)}) "
            f"| {_fmt(total.win_pct, 1)} | {_fmt(total.expectancy_r, 3)} | {split} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sweeps = sub.add_parser("sweeps", help="write the variants files")
    sweeps.add_argument("--out", type=Path, required=True)
    loo = sub.add_parser("loo", help="the leave-one-out table of a logged sweep")
    loo.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "sweeps":
        for path in write_sweeps(args.out):
            print(path)
        return 0
    root = Path(os.environ.get("HAT_DATA_ROOT", "data_system/hat")) / "backtests" / "runs"
    runs = logged_runs(inside_cwd(args.log).read_text().splitlines())
    if "shadow" not in runs:
        print("no complete shadow run in the log", file=sys.stderr)
        return 1
    print(loo_report(runs, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
