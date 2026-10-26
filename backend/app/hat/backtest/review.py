"""The review of a candidate run (docs/backtests/process.md, step 6):

    python -m app.hat.backtest.review <run_id> --criteria NOTES [--sweep PLATEAU_SWEEP]
        [--validates IN_SAMPLE_RUN_ID]

computes every promotion criterion of the strategy's `hat-criteria` block in NOTES (`criteria`)
from the run folders and the trial registry, and writes the pass/fail table into the run's
folder (`criteria.json`, `criteria.md`, with the block and its sha256), then `review_request.md`:
a self-contained prompt for an independent reviewer agent, with the paths it reads (the run
folder and its files, its audits and the owner's explanation of the data audit's warnings, its
base and the run it validates, the result standard, the process and the criteria table) and
what to write back (`audit_report.md` in the same folder). The in-sample criteria are read from
the in-sample run: the run itself, or for a hold-out run the run it validates (`--validates`,
by default its `data.validates_run_id`); the hold-out criteria from the hold-out run that
validates it. The code calls no model: the orchestrator hands the request to a review agent.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.paths import PROJECT_ROOT

from ..data.audit import explanation_of
from . import criteria as crit
from . import folder, trials
from .evidence import read_evidence, run_dsr

__all__ = ["CRITERIA_JSON", "CRITERIA_MD", "REQUEST", "REPORT", "evaluate", "request_text"]

REQUEST = "review_request.md"
REPORT = "audit_report.md"
CRITERIA_JSON = "criteria.json"
CRITERIA_MD = "criteria.md"
HOLD_OUT = "hold_out"
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
    "dsr_n_trials_effective",
    "pbo",
    "cost_share_pct",
    "top5_trades_pnl_share_pct",
    "cagr_ex_top5_trades_pct",
    "trades_over_5pct_adv",
)


def _status(doc: Mapping[str, Any] | None) -> str:
    return "absent" if doc is None else str(doc.get("status"))


def _id(run: Mapping[str, Any] | None) -> str | None:
    found = None if run is None else (run.get("identity") or {}).get("run_id")
    return found if isinstance(found, str) else None


def _rows(text: str | None) -> list[dict[str, str]]:
    return [] if not text else list(csv.DictReader(io.StringIO(text)))


def _not_passed(doc: Mapping[str, Any] | None) -> str:
    """The names of an audit document's checks that did not pass, or `none`."""
    names = [
        f"`{c.get('name')}`"
        for c in (doc or {}).get("checks") or []
        if isinstance(c, Mapping) and c.get("status") != "pass"
    ]
    return ", ".join(names) or "none"


def _dsr_now(t: Mapping[str, Any]) -> str:
    if t.get("dsr_prob_now") is None:
        return "DSR now: no admitted evidence"
    return f"DSR now {t['dsr_prob_now']} at {t['dsr_n_trials_now']} trials"


def evaluate(
    root: Path,
    run: Mapping[str, Any],
    criteria: crit.Criteria,
    *,
    source: Path,
    sweep: str | None = None,
    validates: str | None = None,
) -> dict[str, Any]:
    """`criteria.json` of `run`: every criterion computed from the folders under `root` and the
    trial registry, its rows and its overall status (`criteria.overall`). `review_bad_validates`
    (ValueError) when the in-sample run of a hold-out run cannot be read, `review_validates_
    in_sample` when `validates` is given for an in-sample run."""
    run_id, data = _id(run), run.get("data") or {}
    runs = folder.list_runs(root)
    if data.get("split") == HOLD_OUT:
        in_id = validates or data.get("validates_run_id")
        in_sample = None if not isinstance(in_id, str) else folder.read_run(root, in_id)
        if in_sample is None:
            raise ValueError("review_bad_validates")
        hold_out: Mapping[str, Any] | None = run
    elif validates is not None:
        raise ValueError("review_validates_in_sample")
    else:
        in_sample, hold_out = run, None
    in_id = _id(in_sample)
    reads = [
        r
        for r in runs
        if (r.get("data") or {}).get("split") == HOLD_OUT
        and (r.get("data") or {}).get("validates_run_id") == in_id
        and (r.get("identity") or {}).get("status") == "complete"
    ]
    if hold_out is None and reads:
        hold_out = reads[0]  # newest first
    key = str(trials.family_of(in_sample, None))
    total, prior = trials.counts(trials.read_registry(root), key)
    found = None if in_id is None else read_evidence(root, in_id)
    now = None if found is None else run_dsr(root, found)
    neighbours = [r for r in runs if (r.get("identity") or {}).get("sweep_id") == sweep]
    rows = [
        *crit.in_sample_rows(
            criteria, in_sample, _rows(folder.read_csv(root, in_id or "", "trades"))
        ),
        *crit.trial_rows(criteria, in_sample, key, total, prior),
        *crit.plateau_rows(criteria, in_sample, sweep, neighbours),
        *crit.hold_out_rows(criteria, in_sample, hold_out, len(reads)),
    ]
    return {
        "run_id": run_id,
        "in_sample_run_id": in_id,
        "hold_out_run_id": _id(hold_out),
        "plateau_sweep": sweep,
        "criteria": {
            "source": str(source),
            "digest": criteria.digest,
            "strategy": criteria.strategy,
            "text": criteria.text,
        },
        "trials": {
            "family": key,
            "trials": total,
            "prior": prior,
            "budget": criteria.trial_budget,
            # the in-sample run's DSR at the family's count now (`evidence.run_dsr`), as the
            # gates read it; None when the run has no evidence the gates admit
            "dsr_prob_now": None if now is None else round(now[0].prob, 6),
            "dsr_n_trials_now": None if now is None else now[1],
        },
        "status": crit.overall(rows),
        "rows": [asdict(r) for r in rows],
    }


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value).replace("|", "/")


def criteria_markdown(
    doc: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    data_audit: Mapping[str, Any] | None,
    explained: Path | None,
) -> str:
    """`criteria.md`: the overall status, the criteria table, the audits' checks that did not
    pass (with the explanation of the data audit's) and the criteria block it applied."""
    c, t = doc["criteria"], doc["trials"]
    lines = [
        f"# Promotion criteria: run {doc['run_id']}",
        "",
        f"**{doc['status']}**. Criteria `{c['strategy']}` sha256 `{c['digest']}` from"
        f" `{c['source']}`; in-sample run `{doc['in_sample_run_id']}`, hold-out run"
        f" `{doc['hold_out_run_id'] or '-'}`, plateau sweep `{doc['plateau_sweep'] or '-'}`.",
        f"Trials in `{t['family']}`: {t['trials']} ({t['prior']} prior, seeded), budget"
        f" {t['budget']} for the family's own runs; {_dsr_now(t)}.",
        "",
        "| section | criterion | value | threshold | status | note |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['section']} | {_cell(r['name'])} | {_cell(r['value'])} | {_cell(r['threshold'])}"
        f" | {r['status']} | {_cell(r['note'])} |"
        for r in doc["rows"]
    ]
    lines += [
        "",
        "## Audits",
        "",
        f"- run audit: {_status(audit)}; checks not passing: {_not_passed(audit)}",
        f"- data audit: {_status(data_audit)}; checks not passing:"
        f" {_not_passed(data_audit)}; owner's explanation:"
        f" {'`' + str(explained) + '`' if explained else 'none'}",
        "",
        "## Criteria block",
        "",
        f"```{crit.FENCE}",
        c["text"].rstrip("\n"),
        "```",
        "",
    ]
    return "\n".join(lines)


def request_text(
    run: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    data_audit: Mapping[str, Any] | None,
    paths: Mapping[str, Path | None],
    criteria: Mapping[str, Any] | None = None,
) -> str:
    """The review request of a run (`run.json`, its run and data audits) with the paths the
    reviewer reads (`paths`: `folder`, `standard`, `process`, `criteria` (the criteria table),
    `explained` (the data audit's explanation), `base`, `validates`; None where there is none)
    and the evaluated criteria (`criteria.json`, None without criteria)."""
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
        f"- run audit: {_status(audit)}; data audit: {_status(data_audit)}"
        f" (checks not passing: {_not_passed(data_audit)}; owner's explanation:"
        f" {'`' + str(paths['explained']) + '`' if paths.get('explained') else 'none'})",
        "- headline (verify): "
        + ", ".join(f"{k} {metrics.get(k)}" for k in _HEADLINE if k in metrics),
    ]
    if criteria is not None:
        t = criteria["trials"]
        lines += [
            f"- trials: {t['trials']} in `{t['family']}` ({t['prior']} prior, seeded from deleted"
            f" runs), budget {t['budget']}; at completion the DSR counted"
            f" {metrics.get('dsr_n_trials')} and scored at"
            f" {metrics.get('dsr_n_trials_effective')}; {_dsr_now(t)}",
            f"- promotion criteria (computed by `python -m app.hat.backtest.review`):"
            f" **{criteria['status']}**, criteria sha256 `{criteria['criteria']['digest']}`",
        ]
    lines += [
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
        lines.append(
            f"- promotion criteria, fixed before the runs, and the table computed from them:"
            f" `{paths['criteria']}` (machine form: `{out / CRITERIA_JSON}`)"
        )
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
        "1. Every `fail` and `warn` of both audits: is each explained (the data audit's in the"
        " owner's explanation file), and does any make the result unreliable?",
        "2. The criteria table: recompute each row from `run.json`, `trades.csv` and"
        " `equity.csv` where you can. A `fail` or `not_run` row means the run is not a candidate.",
        "3. Overfitting: the trial count (prior trials included) and budget, `dsr_prob` at"
        " `dsr_n_trials_effective`, `pbo`, the plateau rows, and whether the hold-out was read"
        " more than once.",
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


def write_review(
    root: Path,
    run_id: str,
    criteria: Path | None = None,
    *,
    sweep: str | None = None,
    validates: str | None = None,
) -> Path:
    """Evaluate the criteria into the run's folder (`criteria.json`, `criteria.md`) and write its
    `review_request.md`. Refused (ValueError): `review_unknown_run` without its folder,
    `review_criteria_missing` when `criteria` names no file, the block's own refusals
    (`criteria.extract`, `criteria.parse`), `review_criteria_strategy` for another strategy's
    criteria, and `evaluate`'s."""
    run = folder.read_run(root, run_id)
    out = folder.run_dir(root, run_id)
    if run is None or out is None:
        raise ValueError("review_unknown_run")
    if criteria is not None and not criteria.is_file():
        raise ValueError("review_criteria_missing")
    identity, data = run.get("identity") or {}, run.get("data") or {}
    audit, data_audit = (
        folder.read_doc(root, run_id, "audit"),
        folder.read_doc(root, run_id, "data_audit"),
    )
    explained = explanation_of(root, (data.get("audit") or {}).get("digest"))
    evaluated = None
    if criteria is not None:
        parsed = crit.parse(crit.extract(criteria.read_text(encoding="utf-8")))
        if parsed.strategy != identity.get("strategy"):
            raise ValueError("review_criteria_strategy")
        evaluated = evaluate(
            root, run, parsed, source=criteria.resolve(), sweep=sweep, validates=validates
        )
        (out / CRITERIA_JSON).write_text(json.dumps(evaluated, indent=1), encoding="utf-8")
        (out / CRITERIA_MD).write_text(
            criteria_markdown(evaluated, audit, data_audit, explained), encoding="utf-8"
        )
    base = identity.get("base_run_id")
    in_sample = None if evaluated is None else evaluated["in_sample_run_id"]
    validated = data.get("validates_run_id") or (in_sample if in_sample != run_id else None)
    docs = PROJECT_ROOT / "docs" / "backtests"
    text = request_text(
        run,
        audit,
        data_audit,
        {
            "folder": out,
            "standard": docs / "result-standard.md",
            "process": docs / "process.md",
            "criteria": None if evaluated is None else out / CRITERIA_MD,
            "explained": explained,
            "base": None if base is None else folder.run_dir(root, base),
            "validates": None if validated is None else folder.run_dir(root, validated),
        },
        evaluated,
    )
    path = out / REQUEST
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the command line
    """python -m app.hat.backtest.review <run_id> [--criteria NOTES] [--sweep ID]
    [--validates RUN_ID]: evaluate the criteria, write the review request and print the
    criteria table and the request's path."""
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.review")
    parser.add_argument("run_id")
    parser.add_argument("--criteria", type=Path, default=None)
    parser.add_argument("--sweep", default=None, help="the plateau sweep of the run")
    parser.add_argument("--validates", default=None, help="a hold-out run's in-sample run")
    args = parser.parse_args(argv)
    try:
        path = write_review(
            operator_settings().data_root,
            args.run_id,
            args.criteria,
            sweep=args.sweep,
            validates=args.validates,
        )
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"review refused: {exc}\n")
        return 2
    table = path.with_name(CRITERIA_MD)
    if args.criteria is not None and table.is_file():
        sys.stdout.write(table.read_text(encoding="utf-8"))
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
