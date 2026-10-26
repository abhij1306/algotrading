"""Compare runs side by side (docs/backtests/process.md):

    python -m app.hat.backtest.compare <run_id> [<run_id> ...]
    python -m app.hat.backtest.compare --sweep <sweep_id>

prints one Markdown table row per run, read from its folder only: label, change and base, whether
the change bound, how many knobs it changed against its base (`5b`: a declared bundle of five;
`run_audit.changed_of`), its risk policy version (`3!2`: version 3 against a base under 2, the
policy drifted), the run and data audit statuses, and the headline metrics.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import folder
from .run_audit import changed_of

__all__ = ["COLUMNS", "policy_versions", "row_of", "table"]

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
    ("MAR", "metrics", "mar"),
    ("sharpe", "metrics", "sharpe"),
    ("DSR", "metrics", "dsr_prob"),
    ("trials", "metrics", "dsr_n_trials"),
    ("N_eff", "metrics", "dsr_n_trials_effective"),
    ("PBO", "metrics", "pbo"),
    ("top5%", "metrics", "top5_trades_pnl_share_pct"),
    ("cost%", "metrics", "cost_share_pct"),
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


def row_of(
    run: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    policy: tuple[object, object] = (None, None),
) -> list[str]:
    """One run's cells: its short id, the `COLUMNS`, the knobs it changed, its and its base's
    risk policy versions (`policy`) and its audits' statuses."""
    run_id = str((run.get("identity") or {}).get("run_id", ""))
    cells = [run_id[:8]]
    cells += [_cell((run.get(block) or {}).get(name)) for _, block, name in COLUMNS]
    cells += [_knobs(run), _policy(*policy)]
    data_audit = ((run.get("data") or {}).get("audit") or {}).get("status")
    cells.append(f"{_cell(None if audit is None else audit.get('status'))}/{_cell(data_audit)}")
    return cells


def table(rows: Sequence[Sequence[str]]) -> str:
    """The Markdown table of `rows` (`row_of`)."""
    header = ["run", *(h for h, _, _ in COLUMNS), "knobs", "policy", "audit/data"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def compare(root: Path, run_ids: Sequence[str], sweep_id: str | None) -> str:
    """The table of the named runs, or of a sweep's runs oldest first."""
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
        rows.append(row_of(r, folder.read_doc(root, run_id, "audit"), policy_versions(root, r)))
    return table(rows)


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
    args = parser.parse_args(argv)
    if not args.run_ids and args.sweep is None:
        parser.error("name run ids or --sweep")
    sys.stdout.write(compare(operator_settings().data_root, args.run_ids, args.sweep))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
