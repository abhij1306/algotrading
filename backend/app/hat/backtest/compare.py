"""Compare runs side by side (docs/backtests/process.md):

    python -m app.hat.backtest.compare <run_id> [<run_id> ...]
    python -m app.hat.backtest.compare --sweep <sweep_id>

prints one Markdown table row per run, read from its folder only: label, change and base, whether
the change bound, the run and data audit statuses, and the headline metrics.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import folder

__all__ = ["COLUMNS", "row_of", "table"]

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


def row_of(run: Mapping[str, Any], audit: Mapping[str, Any] | None) -> list[str]:
    """One run's cells: its short id, the `COLUMNS` and its audits' statuses."""
    run_id = str((run.get("identity") or {}).get("run_id", ""))
    cells = [run_id[:8]]
    cells += [_cell((run.get(block) or {}).get(name)) for _, block, name in COLUMNS]
    data_audit = ((run.get("data") or {}).get("audit") or {}).get("status")
    cells.append(f"{_cell(None if audit is None else audit.get('status'))}/{_cell(data_audit)}")
    return cells


def table(rows: Sequence[Sequence[str]]) -> str:
    """The Markdown table of `rows` (`row_of`)."""
    header = ["run", *(h for h, _, _ in COLUMNS), "audit/data"]
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
    return table(
        [
            row_of(r, folder.read_doc(root, str((r.get("identity") or {}).get("run_id")), "audit"))
            for r in runs
        ]
    )


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
