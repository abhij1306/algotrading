"""Compare runs side by side (docs/backtests/process.md):

    python -m app.hat.backtest.compare <run_id> [<run_id> ...] [--criteria NOTES]
    python -m app.hat.backtest.compare --sweep <sweep_id> [--criteria NOTES]

prints one Markdown table row per run, read from its folder only: label, change and base, whether
the change bound, how many knobs it changed against its base (`5b`: a declared bundle of five;
`run_audit.changed_of`), its risk policy version (`3!2`: version 3 against a base under 2, the
policy drifted), the run and data audit statuses, and the headline metrics with the promotion
criteria's (longest drawdown in sessions, the benchmark's CAGR, the CAGR without the top five
trades, the entries above 5% of ADV), and the invested share (the equity held in positions: the
mean over sessions, and the highest session's from `equity.csv`). With `--criteria` (the
strategy's notes, their `hat-criteria` block) a second table gives each run's criteria, pass or
fail, as the review computes them (`criteria.in_sample_rows`, the DSR and PBO at the family
today; a hold-out run's in-sample rows are its validated run's, beside its own hold-out rows),
and the family's own trials against the budget.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import criteria as crit
from . import folder, trials
from .review import scores_now, trade_rows
from .run_audit import changed_of

__all__ = [
    "COLUMNS",
    "criteria_rows",
    "criteria_table",
    "max_invested",
    "policy_versions",
    "row_of",
    "table",
]

HOLD_OUT = "hold_out"

# (header, block, field) of each column after the run id
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("variant", "identity", "variant"),
    ("change", "identity", "change"),
    ("status", "identity", "status"),
    ("no_op", "binding", "no_op"),
    ("trades", "metrics", "trades"),
    ("exp_R", "metrics", "expectancy_r"),
    ("win%", "metrics", "win_pct"),
    ("CAGR%", "metrics", "cagr_pct"),
    ("maxDD%", "metrics", "max_dd_pct"),
    ("longest_dd", "metrics", "longest_dd_sessions"),
    ("MAR", "metrics", "mar"),
    ("sharpe", "metrics", "sharpe"),
    ("DSR", "metrics", "dsr_prob"),
    ("trials", "metrics", "dsr_n_trials"),
    ("N_eff", "metrics", "dsr_n_trials_effective"),
    ("PBO", "metrics", "pbo"),
    ("top5%", "metrics", "top5_trades_pnl_share_pct"),
    ("cost%", "metrics", "cost_share_pct"),
    ("bench%", "metrics", "benchmark_cagr_pct"),
    ("cagr_ex5%", "metrics", "cagr_ex_top5_trades_pct"),
    (">5%adv", "metrics", "trades_over_5pct_adv"),
    ("inv%", "metrics", "invested_pct"),
    ("avg_pos", "metrics", "avg_open_positions"),
    ("size%", "metrics", "avg_position_size_pct"),
    ("flat%", "metrics", "zero_position_days_pct"),
    ("PF", "metrics", "profit_factor"),
    ("payoff_R", "metrics", "payoff_ratio_r"),
    ("cagr/inv%", "metrics", "cagr_per_invested_pct"),
    ("mfe_cap", "metrics", "mfe_capture"),
)


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 10 else f"{value:.1f}"
    return str(value).replace("|", "/")


def _knobs(run: Mapping[str, Any]) -> str:
    """The knobs a variant changed against its base (`-` for a baseline), `b` a bundle."""
    identity = run.get("identity") or {}
    if identity.get("base_run_id") is None:
        return "-"
    changed, _ = changed_of(run)
    return f"{len(changed)}{'b' if identity.get('bundle') else ''}"


def _policy(mine: object, base: object) -> str:
    """The run's risk policy version, and its base's after `!` when they differ."""
    if base is None or base == mine:
        return _cell(mine)
    return f"{_cell(mine)}!{_cell(base)}"


def max_invested(equity: str | None) -> float | None:
    """The highest session's invested share (`invested_pct`) in a run's `equity.csv` text; None
    without the file or a value."""
    if equity is None:
        return None
    values = [
        float(v) for row in csv.DictReader(io.StringIO(equity)) if (v := row.get("invested_pct"))
    ]
    return max(values, default=None)


def row_of(
    run: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    policy: tuple[object, object] = (None, None),
    invested_max: float | None = None,
) -> list[str]:
    """One run's cells: its short id, the `COLUMNS`, its highest session's invested share
    (`max_invested`), the knobs it changed, its and its base's risk policy versions (`policy`)
    and its audits' statuses."""
    run_id = str((run.get("identity") or {}).get("run_id", ""))
    cells = [run_id[:8]]
    cells += [_cell((run.get(block) or {}).get(name)) for _, block, name in COLUMNS]
    cells += [_cell(invested_max), _knobs(run), _policy(*policy)]
    data_audit = ((run.get("data") or {}).get("audit") or {}).get("status")
    cells.append(f"{_cell(None if audit is None else audit.get('status'))}/{_cell(data_audit)}")
    return cells


def table(rows: Sequence[Sequence[str]]) -> str:
    """The Markdown table of `rows` (`row_of`)."""
    header = ["run", *(h for h, _, _ in COLUMNS), "inv_max%", "knobs", "policy", "audit/data"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _column(row: crit.Row) -> str:
    return row.name if row.section == "in_sample" else f"{row.section} {row.name}"


def criteria_table(runs: Sequence[tuple[str, Sequence[crit.Row]]]) -> str:
    """The Markdown table of each run's (short id, criteria rows): one column per criterion
    (a hold-out row's named by its section too), its status, and the run's overall status
    (`criteria.overall`)."""
    names = list(dict.fromkeys(_column(r) for _, rows in runs for r in rows))
    lines = [
        "| " + " | ".join(["run", *names, "overall"]) + " |",
        "|" + "---|" * (len(names) + 2),
    ]
    for run_id, rows in runs:
        status = {_column(r): r.status for r in rows}
        cells = [run_id, *(status.get(n, "-") for n in names), crit.overall(rows)]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


def criteria_rows(
    c: crit.Criteria,
    run: Mapping[str, Any],
    in_sample: Mapping[str, Any] | None,
    in_sample_rows: Callable[[Mapping[str, Any]], list[crit.Row]],
    reads: int,
) -> list[crit.Row]:
    """A run's criteria rows as the review computes them: an in-sample run's in-sample rows
    (`in_sample_rows`, which reads its trades and scores); a hold-out run's are those of the
    in-sample run it validates (`in_sample`, None when it cannot be read: a failing row) and
    its hold-out rows (`reads`: the hold-out runs validating that run)."""
    if (run.get("data") or {}).get("split") != HOLD_OUT:
        return in_sample_rows(run)
    if in_sample is None:
        validates = (run.get("data") or {}).get("validates_run_id")
        return [crit.Row("hold_out", "in-sample run", validates, "readable", crit.FAIL)]
    return [*in_sample_rows(in_sample), *crit.hold_out_rows(c, in_sample, run, reads)]


def _criteria(root: Path, runs: Sequence[Mapping[str, Any]], c: crit.Criteria) -> str:
    """The criteria table of `runs` and the line of the family's own trials and budget."""

    def in_sample_rows(run: Mapping[str, Any]) -> list[crit.Row]:
        run_id = str((run.get("identity") or {}).get("run_id"))
        trades = trade_rows(folder.read_csv(root, run_id, "trades"))
        return crit.in_sample_rows(c, run, trades, scores_now(root, run)[0])

    held = [r for r in runs if (r.get("data") or {}).get("split") == HOLD_OUT]
    reads = Counter(
        (r.get("data") or {}).get("validates_run_id")
        for r in (folder.list_runs(root) if held else [])
        if (r.get("data") or {}).get("split") == HOLD_OUT
        and (r.get("identity") or {}).get("status") == folder.COMPLETE
    )
    evaluated = []
    for r in runs:
        validates = (r.get("data") or {}).get("validates_run_id")
        in_sample = folder.read_run(root, validates) if isinstance(validates, str) else None
        rows = criteria_rows(c, r, in_sample, in_sample_rows, reads[validates])
        evaluated.append((str((r.get("identity") or {}).get("run_id"))[:8], rows))
    key = trials.family_key(c.strategy, c.universe, *c.in_sample)
    total, prior = trials.counts(trials.read_registry(root), key)
    budget = (
        f"\ntrial budget: {total - prior} own trials of {c.trial_budget} in {key}"
        f" ({total} with {prior} prior); criteria sha256 {c.digest}\n"
    )
    return criteria_table(evaluated) + budget


def compare(
    root: Path, run_ids: Sequence[str], sweep_id: str | None, c: crit.Criteria | None = None
) -> str:
    """The table of the named runs, or of a sweep's runs oldest first, and with criteria `c`
    their criteria table."""
    if sweep_id is not None:
        runs = [
            r
            for r in reversed(folder.list_runs(root))
            if (r.get("identity") or {}).get("sweep_id") == sweep_id
        ]
    else:
        runs = [r for r in (folder.read_run(root, i) for i in run_ids) if r is not None]
    rows = []
    for r in runs:
        run_id = str((r.get("identity") or {}).get("run_id"))
        audit = folder.read_doc(root, run_id, "audit")
        invested = max_invested(folder.read_csv(root, run_id, "equity"))
        rows.append(row_of(r, audit, policy_versions(root, r), invested))
    return table(rows) + ("" if c is None else "\n" + _criteria(root, runs, c))


def _version_in(root: Path, run_id: object) -> object:
    config = None if run_id is None else folder.read_config(root, str(run_id))
    return None if config is None else (config.get("risk_policy") or {}).get("version")


def policy_versions(root: Path, run: Mapping[str, Any]) -> tuple[object, object]:
    """A run's risk policy version and its base's (None without a base), from `run.json` or,
    for a run that predates them there, from the runs' `config.json`."""
    config = run.get("config") or {}
    base = (run.get("identity") or {}).get("base_run_id")
    mine = config.get("risk_policy_version")
    theirs = config.get("base_risk_policy_version")
    if mine is None:
        mine = _version_in(root, (run.get("identity") or {}).get("run_id"))
    if theirs is None and base is not None:
        theirs = _version_in(root, base)
    return mine, theirs


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the command line
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.compare")
    parser.add_argument("run_ids", nargs="*")
    parser.add_argument("--sweep", default=None)
    parser.add_argument("--criteria", type=argparse.FileType("r", encoding="utf-8"), default=None)
    args = parser.parse_args(argv)
    if not args.run_ids and args.sweep is None:
        parser.error("name run ids or --sweep")
    c = None
    if args.criteria is not None:
        with args.criteria as file:
            try:
                c = crit.parse(crit.extract(file.read()))
            except ValueError as exc:
                sys.stderr.write(f"compare refused: {exc}\n")
                return 2
    sys.stdout.write(compare(operator_settings().data_root, args.run_ids, args.sweep, c))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
