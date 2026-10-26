"""The review request of a candidate run (docs/backtests/process.md):

    python -m app.hat.backtest.review <run_id> [--criteria FILE]

writes `review_request.md` in the run's folder: a self-contained prompt for an independent
reviewer agent, with the paths it reads (the run folder and its files, its audits, its base and
the run it validates, the result standard, the process, and the strategy's promotion criteria,
`--criteria`), and what to write back (`audit_report.md` in the same folder). The code calls no
model: the orchestrator hands the file to a review agent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.paths import PROJECT_ROOT

from . import folder

__all__ = ["REQUEST", "REPORT", "request_text"]

REQUEST = "review_request.md"
REPORT = "audit_report.md"
_HEADLINE = (
    "trades",
    "expectancy_r",
    "win_pct",
    "cagr_pct",
    "max_dd_pct",
    "mar",
    "sharpe",
    "dsr_prob",
    "dsr_n_trials",
    "pbo",
    "cost_share_pct",
    "top5_trades_pnl_share_pct",
    "cagr_ex_top5_trades_pct",
    "trades_over_5pct_adv",
)


def _status(doc: Mapping[str, Any] | None) -> str:
    return "absent" if doc is None else str(doc.get("status"))


def request_text(
    run: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    data_audit: Mapping[str, Any] | None,
    paths: Mapping[str, Path | None],
) -> str:
    """The review request of a run (`run.json`, its run and data audits) with the paths the
    reviewer reads (`paths`: `folder`, `standard`, `process`, `criteria`, `base`, `validates`;
    None where there is none)."""
    identity, data = run.get("identity") or {}, run.get("data") or {}
    metrics = run.get("metrics") or {}
    out = paths["folder"]
    lines = [
        f"# Review request: run {identity.get('run_id')}",
        "",
        "You are an independent reviewer of one backtest run of HAT, an NSE swing-trading system.",
        "You did not build the strategy or run the backtest. Decide whether this run is evidence",
        "the strategy may go to its hold-out run (or, for a hold-out run, to paper trading). Read",
        "only the files below; do not run a backtest, change code, or trust a number you have not",
        "checked against the run's own files.",
        "",
        "## The run",
        "",
        f"- strategy `{identity.get('strategy')}`, version `{identity.get('strategy_version')}`,"
        f" engine `{identity.get('engine_commit')}`",
        f"- sweep `{identity.get('sweep_id')}`, variant `{identity.get('variant')}`,"
        f" change: {identity.get('change')}",
        f"- universe `{data.get('universe')}`, {data.get('period_start')} to"
        f" {data.get('period_end')}, split `{data.get('split')}`",
        f"- run audit: {_status(audit)}; data audit: {_status(data_audit)}",
        "- headline (verify): "
        + ", ".join(f"{k} {metrics.get(k)}" for k in _HEADLINE if k in metrics),
        "",
        "## Files",
        "",
        f"- run folder: `{out}` (`run.json`, `config.json`, `trades.csv`, `equity.csv`,"
        " `refusals.csv`, `signals.csv`, `stocks.csv`)",
        f"- run audit: `{out / 'audit.json'}`; data audit: `{out / 'data_audit.json'}`",
        f"- result standard (what every field means): `{paths['standard']}`",
        f"- backtest process (the rules this run must have followed): `{paths['process']}`",
    ]
    if paths.get("criteria") is not None:
        lines.append(f"- promotion criteria, fixed before the runs: `{paths['criteria']}`")
    else:
        lines.append(
            "- promotion criteria: none given. Say so in the report; without criteria fixed"
            " before the runs the verdict is `reject`."
        )
    if paths.get("base") is not None:
        lines.append(f"- base run (the variant's one-line change is against it): `{paths['base']}`")
    if paths.get("validates") is not None:
        lines.append(f"- in-sample run this hold-out validates: `{paths['validates']}`")
    lines += [
        "",
        "## Check",
        "",
        "1. Every `fail` and `warn` of both audits: is each explained, and does any make the"
        " result unreliable?",
        "2. The promotion criteria, one by one, against `run.json` metrics you recompute from"
        " `trades.csv` and `equity.csv` where you can.",
        "3. Overfitting: `dsr_n_trials`, `dsr_prob`, `pbo`, the sweep's trial budget, whether the"
        " parameter sits on a plateau (the neighbouring runs of the sweep, one step either side),"
        " and whether the hold-out was read more than once.",
        "4. Robustness: `by_year`, `by_regime`, concentration (`top5_trades_pnl_share_pct`,"
        " `cagr_ex_top5_trades_pct`, `stocks.csv`), liquidity (`position_pct_adv`), costs.",
        "5. Realism: the fills against `config.json` conventions and known gaps"
        " (`known_gaps`), refusals that would change the result (`refusals.csv` forward R).",
        "",
        "## Write",
        "",
        f"`{out / REPORT}`: a verdict line first, `promote`, `hold` or `reject`, then one section"
        " per check above with the evidence (file, column, numbers), then what would change the"
        " verdict. Keep it under two pages.",
        "",
    ]
    return "\n".join(lines)


def write_request(root: Path, run_id: str, criteria: Path | None = None) -> Path:
    """Write the run's `review_request.md`; `review_unknown_run` (ValueError) without its
    folder, `review_criteria_missing` when `criteria` names no file."""
    run = folder.read_run(root, run_id)
    out = folder.run_dir(root, run_id)
    if run is None or out is None:
        raise ValueError("review_unknown_run")
    if criteria is not None and not criteria.is_file():
        raise ValueError("review_criteria_missing")
    identity, data = run.get("identity") or {}, run.get("data") or {}
    base, validates = identity.get("base_run_id"), data.get("validates_run_id")
    docs = PROJECT_ROOT / "docs" / "backtests"
    text = request_text(
        run,
        folder.read_doc(root, run_id, "audit"),
        folder.read_doc(root, run_id, "data_audit"),
        {
            "folder": out,
            "standard": docs / "result-standard.md",
            "process": docs / "process.md",
            "criteria": None if criteria is None else criteria.resolve(),
            "base": None if base is None else folder.run_dir(root, base),
            "validates": None if validates is None else folder.run_dir(root, validates),
        },
    )
    path = out / REQUEST
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the command line
    """python -m app.hat.backtest.review <run_id> [--criteria FILE]: write the run's review
    request and print its path."""
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.review")
    parser.add_argument("run_id")
    parser.add_argument("--criteria", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        path = write_request(operator_settings().data_root, args.run_id, args.criteria)
    except ValueError as exc:
        sys.stderr.write(f"review refused: {exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
