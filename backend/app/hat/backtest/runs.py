"""Backtest runs and trial counting: recording a run, the files it writes and the family's
figures at its completion. What the runs evidence is read in `evidence`.

A run is recorded before it starts (status `running`), so it counts as a trial of its family
whatever happens next (HAT-BT-030); it ends `completed`, with its metrics and `trades.parquet`, or
`failed`, with a fixed error code. A family is a (strategy, universe) pair (HAT-BT-031,
`evidence.family_key`).

A completed run stores its DSR at its completion (`evidence.run_dsr`: only a recorded run is
scored, G33), writes its daily returns (`returns.parquet`, next to `trades.parquet`) and
records when it completed (`metrics.completed_at`) and the family's probability of backtest
overfitting over the variants with a returns file (`metrics.pbo`).
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..costs import (
    DEFAULT_COST_MODEL,
    DEFAULT_SLIPPAGE_BPS,
    BacktestFills,
    CostError,
    check_slippage,
    load_cost_model,
)
from ..data.store import DataStore, atomic_replace
from ..engine.confidence import buckets
from ..engine.portfolio.common import market_id_of
from ..engine.portfolio.regime_service import regime_profile, stored_regime_table
from ..engine.strategy.config import parse_config
from ..models import HatBacktestBucket, HatBacktestRun, HatBot, HatStrategyVersion
from ..risk.policy import effective_version
from ..risk.policy_upkeep import load_versions
from ..time import utc_now
from .data import MarketData, load_market
from .dsr import MIN_TRIALS, PBO_BLOCKS, VARIANCE_FLOOR, Deflated, pbo
from .evidence import COMPLETED, DSR_DIGITS, RunMetrics, family_key, run_dsr, universe_of
from .loop import run_backtest
from .report import BacktestResult
from .report import trades_parquet as encode_trades
from .spec import REGIME_MULT_POLICY, BacktestError, RunConventions, RunSpec, with_cooldown

__all__ = [
    "FAILED",
    "RETURNS_SCHEMA",
    "RUNNING",
    "RunRequest",
    "backtest_bot",
    "complete_run",
    "fail_run",
    "family_pbo",
    "read_returns",
    "record_backtest",
    "returns_path",
    "start_run",
    "trades_path",
]

RUNNING = "running"
DSR_PLACES = Decimal(1).scaleb(-DSR_DIGITS)
FAILED = "failed"


def trades_path(root: Path, run_id: uuid.UUID) -> Path:
    """`backtests/run_id=<id>/trades.parquet` under the HAT data root (HAT-BT-032)."""
    return root / "backtests" / f"run_id={run_id}" / "trades.parquet"


def returns_path(root: Path, run_id: uuid.UUID) -> Path:
    """`backtests/run_id=<id>/returns.parquet`: the run's daily returns."""
    return root / "backtests" / f"run_id={run_id}" / "returns.parquet"


RETURNS_SCHEMA = pa.schema(
    [("date", pa.date32()), ("equity", pa.decimal128(18, 4)), ("ret", pa.float64())]
)


def _returns_parquet(result: BacktestResult) -> bytes:
    """The run's session closes and daily returns, each against the close before (the first
    against `metrics.initial_equity`; null without it, or after a close at or below 0): the
    series `metrics.returns` describes."""
    initial = RunMetrics.from_json(result.metrics).initial_equity
    previous = None if initial is None else float(initial)
    rows = []
    for day, equity in result.equity:
        value = float(equity)
        rows.append(
            {
                "date": day,
                "equity": equity,
                "ret": None if previous is None or previous <= 0 else value / previous - 1.0,
            }
        )
        previous = value
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=RETURNS_SCHEMA), buffer)
    return buffer.getvalue()


def read_returns(root: Path, run_id: uuid.UUID) -> dict[date, float] | None:
    """A run's daily returns by date; None when it has no returns file (an older run) or
    the file cannot be read as one (`RETURNS_SCHEMA`)."""
    path = returns_path(root, run_id)
    if not path.is_file():
        return None
    try:
        table = pq.read_table(path)
        if not table.schema.equals(RETURNS_SCHEMA):
            return None
    except (OSError, pa.ArrowException):
        return None
    return {
        d: r
        for d, r in zip(
            table.column("date").to_pylist(), table.column("ret").to_pylist(), strict=True
        )
        if r is not None
    }


def _completed(session: Session, family: str) -> list[HatBacktestRun]:
    return list(
        session.scalars(
            select(HatBacktestRun)
            .where(HatBacktestRun.family_key == family, HatBacktestRun.status == COMPLETED)
            .order_by(HatBacktestRun.id)
        ).all()
    )


def _dsr_json(deflated: Deflated, trials: int, variance: float) -> dict[str, Any]:
    return {
        "prob": str(Decimal(repr(deflated.prob)).quantize(DSR_PLACES)),
        "sr0": deflated.sr0,
        "reason": deflated.reason,
        "trials": trials,
        "variance": variance,
        # the floors the probability was read through (N15)
        "trials_floor": MIN_TRIALS,
        "variance_floor": VARIANCE_FLOOR,
    }


def family_pbo(
    session: Session,
    family: str,
    root: Path,
    blocks: int = PBO_BLOCKS,
    *,
    also: HatBacktestRun | None = None,
) -> dict[str, Any]:
    """The probability of backtest overfitting of a family (`dsr.pbo`, CSCV) over its completed
    runs (and `also`, a run being completed) with a readable returns file, on the sessions
    every one of them has a return for. A run whose series equals an earlier one's on those
    sessions (a re-run of the same variant) is that variant, not another. `prob` is null, with
    a `reason`, with fewer than 2 distinct variants (`pbo_too_few_variants`), fewer than 2
    common sessions per block (`pbo_too_short`) or a series CSCV cannot score
    (`pbo_unscorable`, a value that is not finite)."""
    members = [r for r in _completed(session, family) if also is None or r.id != also.id]
    if also is not None:
        members.append(also)
    series = [found for found in (read_returns(root, r.id) for r in members) if found is not None]
    common = sorted(set.intersection(*(set(s) for s in series))) if series else []
    distinct: list[list[float]] = []
    for s in series:
        row = [s[d] for d in common]
        if row not in distinct:
            distinct.append(row)
    out: dict[str, Any] = {
        "prob": None,
        "runs": len(series),
        "variants": len(distinct),
        "sessions": len(common),
        "blocks": blocks,
        "splits": None,
        "reason": None,
    }
    if len(distinct) < 2:
        return {**out, "reason": "pbo_too_few_variants"}
    if len(common) < 2 * blocks:
        return {**out, "reason": "pbo_too_short"}
    found = pbo(distinct, blocks)
    if found is None:
        return {**out, "reason": "pbo_unscorable"}
    return {
        **out,
        "prob": str(Decimal(repr(found.prob)).quantize(DSR_PLACES)),
        "splits": found.splits,
    }


def start_run(
    session: Session,
    *,
    strategy_version_id: uuid.UUID,
    risk_policy_version_id: uuid.UUID,
    universe_id: uuid.UUID,
    start: date,
    end: date,
    data_snapshot: Mapping[str, Any],
) -> HatBacktestRun:
    """Record a run as `running`: from here it is a trial of its family (HAT-BT-030)."""
    version = session.get(HatStrategyVersion, strategy_version_id)
    if version is None:
        raise BacktestError("backtest_unknown_version")
    run = HatBacktestRun(
        strategy_version_id=strategy_version_id,
        risk_policy_version_id=risk_policy_version_id,
        family_key=family_key(version.strategy_id, universe_id),
        period_start=start,
        period_end=end,
        data_snapshot=dict(data_snapshot),
        status=RUNNING,
        metrics={},
    )
    session.add(run)
    session.flush()
    return run


def complete_run(
    session: Session, run: HatBacktestRun, result: BacktestResult, *, root: Path
) -> HatBacktestRun:
    """Write the run's `trades.parquet` and `returns.parquet` and store its metrics with the DSR
    at this moment's family trial count and variance, the family's PBO with this run among its
    variants, and the completion time."""
    if session.get(HatBacktestRun, run.id) is not run:
        raise BacktestError("backtest_run_unrecorded")
    if run.status != RUNNING:
        raise BacktestError("backtest_run_not_running")
    atomic_replace(
        trades_path(root, run.id), lambda tmp: tmp.write_bytes(encode_trades(result.trades))
    )
    atomic_replace(
        returns_path(root, run.id), lambda tmp: tmp.write_bytes(_returns_parquet(result))
    )
    # everything is computed before the one change of status, so nothing can fail in between
    deflated, trials, variance = run_dsr(session, run, RunMetrics.from_json(result.metrics))
    metrics = {
        **result.metrics,
        "dsr": _dsr_json(deflated, trials, variance),
        "pbo": family_pbo(session, run.family_key, root, also=run),
        "completed_at": utc_now().isoformat(),
    }
    run.metrics = metrics
    run.status = COMPLETED
    session.add_all(bucket_rows(run.id, result))
    session.flush()
    return run


def bucket_rows(run_id: uuid.UUID, result: BacktestResult) -> list[HatBacktestBucket]:
    """The run's confidence buckets (D132): its closed trades by playbook and setup-score
    band, as `engine.confidence.buckets` counts them."""
    return [
        HatBacktestBucket(
            run_id=run_id,
            playbook=playbook,
            band=band,
            trades=stats.trades,
            mean_r=stats.mean_r,
            ci90_low=stats.ci90_low,
            ci90_high=stats.ci90_high,
            win_rate=stats.win_rate,
        )
        for (playbook, band), stats in buckets(
            (t.playbook, t.score, t.r_multiple) for t in result.trades
        ).items()
    ]


def fail_run(session: Session, run: HatBacktestRun, code: str) -> HatBacktestRun:
    """End a run as `failed` with a fixed error code; it stays a trial of its family."""
    run.status = FAILED
    run.metrics = {"error": code}
    session.flush()
    return run


def record_backtest(
    session: Session,
    spec: RunSpec,
    data: MarketData,
    *,
    universe_id: uuid.UUID,
    data_snapshot: Mapping[str, Any],
    root: Path,
) -> HatBacktestRun:
    """Record, run and complete one backtest. A run the engine refuses is stored as `failed`
    (still a trial) and its `BacktestError` raised; any other error leaves the run `failed`
    and raises `BacktestError("backtest_error")` chained to it. A caller that commits after
    catching `BacktestError` keeps the trial (the command line does)."""
    run = start_run(
        session,
        strategy_version_id=spec.strategy_version_id,
        risk_policy_version_id=spec.policy.version_id,
        universe_id=universe_id,
        start=spec.start,
        end=spec.end,
        data_snapshot=data_snapshot,
    )
    try:
        result = run_backtest(spec, data)
        return complete_run(session, run, result, root=root)
    except BacktestError as exc:
        fail_run(session, run, exc.code)
        raise
    except Exception as exc:
        # Whatever went wrong, the run stays a trial of its family (HAT-BT-030).
        fail_run(session, run, "backtest_error")
        raise BacktestError("backtest_error") from exc


@dataclass(frozen=True)
class RunRequest:
    """One backtest of a sleeve (`backtest_bot`): its strategy version (the live one unless
    `version_id` names another of the same strategy), the period, the fills (slippage and cost
    model), the cooldown replacing the policy's for a sweep, whether the run may
    evidence a version, and its conventions."""

    bot_id: uuid.UUID
    start: date
    end: date
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS
    cost_model: str = DEFAULT_COST_MODEL
    version_id: uuid.UUID | None = None
    cooldown_sessions: int | None = None
    for_promotion: bool = False
    conventions: RunConventions = field(default_factory=RunConventions)


def backtest_bot(
    session: Session, store: DataStore, request: RunRequest, *, root: Path
) -> HatBacktestRun:
    """Backtest a sleeve's strategy version on the sleeve's universe under the risk policy in
    force now, over the Parquet store; the `policy` regime rule also reads the regime engine's
    index, VIX and breadth, and sizes with the stored regime table live allocates with. The
    command line's body. Refusals before the run is recorded (an unknown bot or version,
    conventions a promotion run may not use, ...) raise `BacktestError` and record nothing; a
    refusal of the engine is a failed trial."""
    bot = session.get(HatBot, request.bot_id)
    if bot is None:
        raise BacktestError("backtest_unknown_bot")
    version = session.get(HatStrategyVersion, request.version_id or bot.live_version_id)
    if version is None or version.strategy_id != bot.strategy_id:
        raise BacktestError("backtest_unknown_version")
    universe_id = universe_of(session, bot)
    if universe_id is None:
        raise BacktestError("backtest_no_universe")
    policy = effective_version(load_versions(session, bot.user_id), utc_now())
    if policy is None:
        raise BacktestError("backtest_no_policy")
    if request.cooldown_sessions is not None:
        policy = with_cooldown(policy, request.cooldown_sessions)
    config = parse_config(version.config)
    try:
        fills = BacktestFills(
            load_cost_model(request.cost_model), check_slippage(request.slippage_bps)
        )
    except CostError as exc:
        raise BacktestError(exc.code) from None
    spec = RunSpec(  # refuses a promotion run's conventions and a missing base weight
        config=config,
        strategy_version_id=version.id,
        policy=policy,
        start=request.start,
        end=request.end,
        fills=fills,
        for_promotion=request.for_promotion,
        conventions=request.conventions,
        regime_table=stored_regime_table(
            session, market_id_of(session), regime_profile(config.bot)
        ),
    )
    data, snapshot = load_market(
        store,
        session,
        universe_id=universe_id,
        index=config.regime.index,
        start=request.start,
        end=request.end,
        regime_inputs=request.conventions.regime_mult == REGIME_MULT_POLICY,
    )
    return record_backtest(
        session, spec, data, universe_id=universe_id, data_snapshot=snapshot, root=root
    )
