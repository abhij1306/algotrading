"""The backtest command line: `python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to
YYYY-MM-DD [...]` backtests a sleeve over the Parquet store and publishes the run's folder
(`runs`, `folder`); `--strategy-version <id> --universe <id or name>` in place of `--bot` is a
research run with no bot. With `--variants FILE` it runs a sweep of variants over one load of
the market (`runs.sweep_bot`); `--rerun <run_id>` runs a run's `config.json` again
(`runs.rerun`), and with `--split hold_out --from --to` runs it as the hold-out of that run
(`runs.hold_out`). It reads the database in a read-only transaction: no backtest writes to it."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, TextIO

import yaml
from sqlalchemy import text

from app.database import db_session_scope

from ..costs import DEFAULT_COST_MODEL, DEFAULT_SLIPPAGE_BPS
from ..data.store import DataStore
from ..models import HatEvent
from ..money import exact
from ..risk.policy import RiskPolicy
from ..settings import operator_settings
from .fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    PENETRATION,
    SUSPENSION_SESSIONS,
)
from .overrides import OverrideError, Overrides, parse_policy_set, parse_set, read_policy_file
from .runs import (
    HOLD_OUT,
    IN_SAMPLE,
    SPLITS,
    Labels,
    RunOutcome,
    RunRequest,
    backtest_bot,
    hold_out,
    parse_variants,
    rerun,
    sweep_bot,
)
from .slots import SLOT_ORDER_TIME, is_slot_order
from .spec import (
    INITIAL_EQUITY,
    REGIME_MULT_ONE,
    REGIME_MULT_RULES,
    BacktestError,
    RunConventions,
)


def _slot_order_arg(text: str) -> str:  # pragma: no cover - the command line
    if not is_slot_order(text):
        raise argparse.ArgumentTypeError("time, score or random:K (K a whole number)")
    return text


def _sleeve_weight_arg(text: str) -> Decimal:  # pragma: no cover - the command line
    number = exact(text)
    if number is None:
        raise argparse.ArgumentTypeError("a decimal in (0, 1], e.g. 0.5")
    return number


def _capital_arg(text: str) -> Decimal:  # pragma: no cover - the command line
    number = exact(text)
    if number is None or number <= 0:
        raise argparse.ArgumentTypeError("rupees above 0, e.g. 2500000")
    return number


def _set_arg(text: str) -> tuple[str, Any]:  # pragma: no cover - the command line
    try:
        return parse_set(text)
    except OverrideError as exc:
        raise argparse.ArgumentTypeError(f"path=value, e.g. exits[2].at_r=3 ({exc.code})") from None


def _policy_set_arg(text: str) -> tuple[str, Any]:  # pragma: no cover - the command line
    try:
        return parse_policy_set(text)
    except OverrideError as exc:
        raise argparse.ArgumentTypeError(
            f"path=value, e.g. kill.bot_drawdown_pct=0.15 ({exc.code})"
        ) from None


def _policy_arg(text: str) -> RiskPolicy:  # pragma: no cover - the command line
    try:
        return read_policy_file(text)
    except OverrideError as exc:
        raise argparse.ArgumentTypeError(
            f"a whole risk policy, YAML or JSON ({exc.code})"
        ) from None


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


def _outcome_json(outcome: RunOutcome) -> dict[str, Any]:  # pragma: no cover - the database
    metrics = (outcome.run or {}).get("metrics") or {}
    return {
        "label": outcome.label,
        "run_id": outcome.run_id,
        "status": outcome.status,
        "code": outcome.code,
        "trades": metrics.get("trades"),
        "expectancy_r": metrics.get("expectancy_r"),
        "dsr": metrics.get("dsr_prob"),
        "dsr_n_trials": metrics.get("dsr_n_trials"),
        "dsr_n_trials_effective": metrics.get("dsr_n_trials_effective"),
        "pbo": metrics.get("pbo"),
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.backtest (--bot <id> | --strategy-version <id> --universe <id|name>)
    --from YYYY-MM-DD --to YYYY-MM-DD [--version <id>] [--slippage-bps N] [--cost-model NAME]
    [--cooldown-sessions N]
    [--entry-fill penetration_1_tick|touch] [--slot-order time|score|random:K]
    [--ambiguous optimistic|pessimistic] [--suspension-sessions N] [--sleeve-weight W]
    [--regime-mult 1|policy] [--start-capital RUPEES] [--for-promotion] [--set PATH=VALUE ...]
    [--risk-pct P]
    [--max-positions N] [--max-per-industry N] [--flat-ladder] [--policy FILE]
    [--set-policy PATH=VALUE ...] [--split in_sample|hold_out] [--validates RUN_ID]
    [--sweep-id ID] [--sweep-name NAME] [--variant LABEL] [--base RUN_ID] [--change TEXT]
    [--variants FILE]
    python -m app.hat.backtest --rerun RUN_ID [--split hold_out --from YYYY-MM-DD --to YYYY-MM-DD]

    Backtests the sleeve's live strategy version (or `--version`) on its universe, or in research
    mode `--strategy-version` on `--universe` with no bot (`runs.RunRequest`), over the
    Parquet store under `HAT_DATA_ROOT`, and publishes `backtests/runs/<run_id>/` (`folder`).
    Prints the run id, status, trade count and DSR. The database is read in a read-only
    transaction. The slippage defaults to 20 bps; `--for-promotion` marks a run under the
    conventions live trades by, and is refused at a slippage of 0 (PR 62). A capped day's slots
    go in the estimated trigger order (`--slot-order time`, PR 63). `--ambiguous pessimistic`
    fills a raised stop the day's low reached; a bar-less position is marked down after
    `--suspension-sessions` sessions (PR 64). Entries are sized from the sleeve's nightly
    allocation at `--sleeve-weight` (the policy's base weight by default; a research run of a
    sleeve the policy gives no weight sizes at `spec.RESEARCH_SLEEVE_WEIGHT`) under the regime
    rule `--regime-mult` (PR 68); a `--for-promotion` run needs `--regime-mult policy` (PR 69).
    `--start-capital` is the account's equity on the first day (₹10,00,000 by default).
    Every run first audits its data (`data.audit`) and refuses to start on a failed audit
    (`backtest_data_audit_failed`); a completed run's folder holds its run audit (`audit.json`).
    `--set` (repeatable), `--risk-pct`, `--max-positions`, `--max-per-industry` and
    `--flat-ladder` override the version's config and the policy for this run only
    (`overrides`); a promotion run takes none. `--policy FILE` (YAML or JSON, e.g. from
    `python -m app.hat.admin show-policy`) sizes the run under that whole candidate policy
    instead of the version in force, and `--set-policy` (repeatable) sets one policy field by
    its dotted path; the other policy overrides apply on top. `--split hold_out` needs
    `--validates`, the in-sample run it validates. `--sweep-id`, `--sweep-name`, `--variant`,
    `--base` (a run id) and `--change` label a single run as a sweep's file labels its
    variants. `--variants FILE` (YAML or JSON, `runs.parse_variants`) runs each variant over one
    load of the market, on top of the options given here, and prints one line per variant as it
    finishes; a file argparse cannot open is a usage error, one that does not parse is refused
    (`backtest_sweep_bad_variants`); a variant changing more than one knob against its base
    needs `bundle: true` and a `reason` (`backtest_sweep_not_one_change`). A hold-out run is
    refused by its guards (`discipline.check_hold_out`). `--rerun RUN_ID` takes nothing else,
    or `--split hold_out --from --to`: the run's own config as its hold-out (`_rerun`).
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["--rerun"]:
        return _rerun(args_list)
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
    parser.add_argument("--start-capital", type=_capital_arg, default=INITIAL_EQUITY)
    parser.add_argument("--for-promotion", action="store_true")
    parser.add_argument("--set", dest="sets", type=_set_arg, action="append", default=[])
    parser.add_argument("--risk-pct", type=_risk_pct_arg, default=None)
    parser.add_argument("--max-positions", type=int, default=None)
    parser.add_argument("--max-per-industry", type=int, default=None)
    parser.add_argument("--flat-ladder", action="store_true")
    parser.add_argument("--policy", type=_policy_arg, default=None)
    parser.add_argument(
        "--set-policy", dest="policy_sets", type=_policy_set_arg, action="append", default=[]
    )
    parser.add_argument("--split", choices=SPLITS, default=IN_SAMPLE)
    parser.add_argument("--validates", default=None)
    parser.add_argument("--sweep-id", default=None)
    parser.add_argument("--sweep-name", default=None)
    parser.add_argument("--variant", default=None)
    parser.add_argument("--base", default=None)
    parser.add_argument("--change", default=None)
    parser.add_argument("--variants", type=argparse.FileType("r", encoding="utf-8"), default=None)
    args = parser.parse_args(args_list)
    settings = operator_settings()
    refused: str | None = None
    summary: dict[str, Any] = {}
    with db_session_scope() as session:
        # a backtest writes no row: hat_live is read-only already, hat_state is made so
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
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
                    start_capital=args.start_capital,
                    overrides=Overrides(
                        sets=tuple(args.sets),
                        risk_pct=args.risk_pct,
                        max_positions=args.max_positions,
                        max_per_industry=args.max_per_industry,
                        flat_ladder=args.flat_ladder,
                        policy=args.policy,
                        policy_sets=tuple(args.policy_sets),
                    ),
                ),
                labels=Labels(
                    sweep_id=args.sweep_id,
                    sweep_name=args.sweep_name,
                    variant=args.variant,
                    base=args.base,
                    change=args.change,
                    split=args.split,
                    validates=args.validates,
                ),
                args=tuple(args_list),
            )
            store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
            if args.variants is not None:
                sweep = parse_variants(_variants_doc(args.variants), request.conventions)

                def after(outcome: RunOutcome) -> None:
                    sys.stdout.write(json.dumps(_outcome_json(outcome)) + "\n")
                    sys.stdout.flush()

                sweep_bot(session, store, request, sweep, root=settings.data_root, after=after)
                return 0
            outcome = backtest_bot(session, store, request, root=settings.data_root)
        except BacktestError as exc:
            refused = exc.code
        else:
            summary = _outcome_json(outcome)
    return _report(refused, summary)


def _rerun(argv: Sequence[str]) -> int:  # pragma: no cover - needs the database
    """`--rerun RUN_ID`: the run's `config.json` again (`runs.rerun`); with `--split hold_out
    --from --to`, that config over the window as the run's hold-out (`runs.hold_out`)."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest")
    parser.add_argument("--rerun", required=True)
    parser.add_argument("--split", choices=(HOLD_OUT,), default=None)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    window = (args.start, args.end)
    plain = args.split is None and window == (None, None)
    if not plain and (args.split is None or None in window):
        parser.error("--split hold_out needs --from and --to; a plain --rerun takes neither")
    settings = operator_settings()
    refused: str | None = None
    summary: dict[str, Any] = {}
    with db_session_scope() as session:
        # a backtest writes no row: hat_live is read-only already, hat_state is made so
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        try:
            store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
            if args.split is None:
                outcome = rerun(session, store, args.rerun, root=settings.data_root, args=argv)
            else:
                outcome = hold_out(
                    session, store, args.rerun, window, root=settings.data_root, args=argv
                )
        except BacktestError as exc:
            refused = exc.code
        else:
            summary = _outcome_json(outcome)
    return _report(refused, summary)


def _report(refused: str | None, summary: Mapping[str, Any]) -> int:  # pragma: no cover
    """Print the refusal (exit 2) or the run's summary (exit 0; a failed run exits 2 too)."""
    if refused is not None:
        sys.stderr.write(f"backtest refused: {refused}\n")
        return 2
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    if summary.get("status") != "complete":
        sys.stderr.write(f"backtest refused: {summary.get('code')}\n")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
