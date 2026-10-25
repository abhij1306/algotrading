"""The backtest command line: `python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to
YYYY-MM-DD [...]` backtests a sleeve over the Parquet store and records the run (`runs`);
`--strategy-version <id> --universe <id or name>` in place of `--bot` is a research run with no
bot. With `--variants FILE` it runs a sweep of variants over one load of the market
(`runs.sweep_bot`)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, TextIO

import yaml

from app.database import db_session_scope

from ..costs import DEFAULT_COST_MODEL, DEFAULT_SLIPPAGE_BPS
from ..data.store import DataStore
from ..money import exact
from ..settings import operator_settings
from .fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    PENETRATION,
    SUSPENSION_SESSIONS,
)
from .overrides import OverrideError, Overrides, parse_set
from .runs import RunRequest, SweepOutcome, backtest_bot, parse_variants, sweep_bot
from .slots import SLOT_ORDER_TIME, is_slot_order
from .spec import REGIME_MULT_ONE, REGIME_MULT_RULES, BacktestError, RunConventions


def _slot_order_arg(text: str) -> str:  # pragma: no cover - the command line
    if not is_slot_order(text):
        raise argparse.ArgumentTypeError("time, score or random:K (K a whole number)")
    return text


def _sleeve_weight_arg(text: str) -> Decimal:  # pragma: no cover - the command line
    number = exact(text)
    if number is None:
        raise argparse.ArgumentTypeError("a decimal in (0, 1], e.g. 0.5")
    return number


def _set_arg(text: str) -> tuple[str, Any]:  # pragma: no cover - the command line
    try:
        return parse_set(text)
    except OverrideError as exc:
        raise argparse.ArgumentTypeError(f"path=value, e.g. exits[2].at_r=3 ({exc.code})") from None


def _risk_pct_arg(text: str) -> Decimal:  # pragma: no cover - the command line
    number = exact(text)
    if number is None:
        raise argparse.ArgumentTypeError("a decimal fraction in (0, 1], e.g. 0.005")
    return number


def _variants_doc(file: TextIO) -> object:  # pragma: no cover - the command line
    """The variants file's document (argparse opened it); `backtest_sweep_bad_variants` when it
    cannot be read or parsed."""
    with file:
        try:
            return yaml.safe_load(file)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            raise BacktestError("backtest_sweep_bad_variants") from None


def _outcome_json(outcome: SweepOutcome) -> dict[str, Any]:  # pragma: no cover - the database
    run = outcome.run
    metrics = {} if run is None else run.metrics
    return {
        "label": outcome.label,
        "run_id": None if run is None else str(run.id),
        "status": "refused" if run is None else run.status,
        "code": outcome.code,
        "trades": metrics.get("trades"),
        "expectancy_r": metrics.get("expectancy_r"),
        "dsr": (metrics.get("dsr") or {}).get("prob"),
        "pbo": (metrics.get("pbo") or {}).get("prob"),
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.backtest (--bot <id> | --strategy-version <id> --universe <id|name>)
    --from YYYY-MM-DD --to YYYY-MM-DD [--version <id>] [--slippage-bps N] [--cost-model NAME]
    [--cooldown-sessions N]
    [--entry-fill penetration_1_tick|touch] [--slot-order time|score|random:K]
    [--ambiguous optimistic|pessimistic] [--suspension-sessions N] [--sleeve-weight W]
    [--regime-mult 1|policy] [--for-promotion] [--set PATH=VALUE ...] [--risk-pct P]
    [--max-positions N] [--variants FILE]

    Backtests the sleeve's live strategy version (or `--version`) on its universe, or in research
    mode `--strategy-version` on `--universe` with no bot (`runs.RunRequest`), over the
    Parquet store under `HAT_DATA_ROOT`, records the run as a trial of its family and writes
    `backtests/run_id=<id>/trades.parquet`. Prints the run id, status, trade count and DSR.
    The slippage defaults to 20 bps; `--for-promotion` marks a run `publish_version` may take as
    a version's evidence, and is refused at a slippage of 0 (PR 62). A capped day's slots go in
    the estimated trigger order (`--slot-order time`, PR 63). `--ambiguous pessimistic` fills
    a raised stop the day's low reached; a bar-less position is marked down after
    `--suspension-sessions` sessions (PR 64). Entries are sized from the sleeve's nightly
    allocation at `--sleeve-weight` (the policy's base weight by default; a research run of a
    sleeve the policy gives no weight sizes at `spec.RESEARCH_SLEEVE_WEIGHT`) under the regime
    rule `--regime-mult` (PR 68); a `--for-promotion` run needs `--regime-mult policy` (PR 69).
    `--set` (repeatable), `--risk-pct` and `--max-positions` override the version's config and
    the policy for this run only (`overrides`), recorded in its conventions; a promotion run
    takes none. `--variants FILE` (YAML or JSON, `runs.parse_variants`) runs each variant over
    one load of the market, on top of the options given here, and prints one line per variant;
    a file argparse cannot open is a usage error, one that does not parse is refused
    (`backtest_sweep_bad_variants`).
    """
    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest")
    parser.add_argument("--bot", type=uuid.UUID, default=None)
    parser.add_argument("--universe", default=None)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--version", "--strategy-version", type=uuid.UUID, default=None)
    parser.add_argument("--slippage-bps", type=Decimal, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--cost-model", default=DEFAULT_COST_MODEL)
    parser.add_argument("--cooldown-sessions", type=int, default=None)
    parser.add_argument("--entry-fill", choices=ENTRY_FILL_RULES, default=PENETRATION)
    parser.add_argument("--slot-order", type=_slot_order_arg, default=SLOT_ORDER_TIME)
    parser.add_argument("--ambiguous", choices=AMBIGUOUS_RULES, default=AMBIGUOUS_OPTIMISTIC)
    parser.add_argument("--suspension-sessions", type=int, default=SUSPENSION_SESSIONS)
    parser.add_argument("--sleeve-weight", type=_sleeve_weight_arg, default=None)
    parser.add_argument("--regime-mult", choices=REGIME_MULT_RULES, default=REGIME_MULT_ONE)
    parser.add_argument("--for-promotion", action="store_true")
    parser.add_argument("--set", dest="sets", type=_set_arg, action="append", default=[])
    parser.add_argument("--risk-pct", type=_risk_pct_arg, default=None)
    parser.add_argument("--max-positions", type=int, default=None)
    parser.add_argument("--variants", type=argparse.FileType("r", encoding="utf-8"), default=None)
    args = parser.parse_args(argv)
    settings = operator_settings()
    refused: str | None = None
    summary: dict[str, Any] = {}
    # A refusal is caught inside the session, so a run the engine refused commits as a failed
    # trial of its family instead of rolling back with the error.
    with db_session_scope() as session:
        try:
            request = RunRequest(
                bot_id=args.bot,
                universe=args.universe,
                start=args.start,
                end=args.end,
                slippage_bps=args.slippage_bps,
                cost_model=args.cost_model,
                version_id=args.version,
                cooldown_sessions=args.cooldown_sessions,
                for_promotion=args.for_promotion,
                conventions=RunConventions(
                    entry_fill_rule=args.entry_fill,
                    slot_order=args.slot_order,
                    ambiguous=args.ambiguous,
                    suspension_sessions=args.suspension_sessions,
                    sleeve_weight=args.sleeve_weight,
                    regime_mult=args.regime_mult,
                    overrides=Overrides(
                        sets=tuple(args.sets),
                        risk_pct=args.risk_pct,
                        max_positions=args.max_positions,
                    ),
                ),
            )
            store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
            if args.variants is not None:
                variants = parse_variants(_variants_doc(args.variants), request.conventions)
                outcomes = sweep_bot(
                    session,
                    store,
                    request,
                    variants,
                    root=settings.data_root,
                    after=session.commit,
                )
                for outcome in outcomes:
                    sys.stdout.write(json.dumps(_outcome_json(outcome)) + "\n")
                return 0
            run = backtest_bot(session, store, request, root=settings.data_root)
        except BacktestError as exc:
            refused = exc.code
        else:
            summary = {
                "run_id": str(run.id),
                "status": run.status,
                "family_key": run.family_key,
                "trades": run.metrics.get("trades"),
                "expectancy_r": run.metrics.get("expectancy_r"),
                "dsr": run.metrics.get("dsr"),
            }
    if refused is not None:
        sys.stderr.write(f"backtest refused: {refused}\n")
        return 2
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
