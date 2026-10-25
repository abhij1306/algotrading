"""The backtest command line: `python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to
YYYY-MM-DD [...]` backtests a sleeve over the Parquet store and records the run (`runs`)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

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
from .runs import RunRequest, backtest_bot
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


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to YYYY-MM-DD
    [--version <id>] [--slippage-bps N] [--cost-model NAME] [--cooldown-sessions N]
    [--entry-fill penetration_1_tick|touch] [--slot-order time|score|random:K]
    [--ambiguous optimistic|pessimistic] [--suspension-sessions N] [--sleeve-weight W]
    [--regime-mult 1|policy] [--for-promotion]

    Backtests the sleeve's live strategy version (or `--version`) on its universe over the
    Parquet store under `HAT_DATA_ROOT`, records the run as a trial of its family and writes
    `backtests/run_id=<id>/trades.parquet`. Prints the run id, status, trade count and DSR.
    The slippage defaults to 20 bps; `--for-promotion` marks a run `publish_version` may take as
    a version's evidence, and is refused at a slippage of 0 (PR 62). A capped day's slots go in
    the estimated trigger order (`--slot-order time`, PR 63). `--ambiguous pessimistic` fills
    a raised stop the day's low reached; a bar-less position is marked down after
    `--suspension-sessions` sessions (PR 64). Entries are sized from the sleeve's nightly
    allocation at `--sleeve-weight` (the policy's base weight by default) under the regime rule
    `--regime-mult` (PR 68); a `--for-promotion` run needs `--regime-mult policy` (PR 69).
    """
    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest")
    parser.add_argument("--bot", type=uuid.UUID, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--version", type=uuid.UUID, default=None)
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
                ),
            )
            store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
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
