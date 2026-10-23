"""Backtest runs, trial counting and the evidence they give (09 §3, §4, §6).

A run is recorded before it starts (status `running`), so it counts as a trial of its family
whatever happens next (HAT-BT-030); it ends `completed`, with its metrics and `trades.parquet`, or
`failed`, with a fixed error code. A family is a (strategy, universe) pair (HAT-BT-031): every
version and parameter variant of one strategy on one universe shares it, so a new version never
escapes the trial penalty.

The deflated Sharpe probability depends on the family's trial count N and the variance V of its
trial Sharpe ratios, which grow after a run is recorded. A run's stored DSR is the value at its
completion; the evidence the lifecycle reads is recomputed from the run's return moments at the
family's current N and V (HAT-BT-060), so an old run never passes the gate on a stale count.

PR 66 (G33, N15): a DSR is computed only for a recorded run (`run_dsr` refuses a run that is
not a `hat_backtest_run` row, so a result run outside `record_backtest` is never scored as
evidence), always through the family floors of `dsr.family_deflated_sharpe`. Each completed run
also writes its daily returns (`returns.parquet`, next to `trades.parquet`) and records when it
completed (`metrics.completed_at`) and the family's probability of backtest overfitting over the
variants with a returns file (`metrics.pbo`). The sleeve's evidence is its newest run by
completion time, not by start.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.store import DataStore, atomic_replace
from ..engine.backtest import (
    LOWER_CIRCUIT_RULE,
    REGIME_MULT_ONE,
    REGIME_MULT_POLICY,
    BacktestError,
    BacktestResult,
    MarketData,
    RunSpec,
    check_conventions,
    run_backtest,
    sleeve_weight,
    with_cooldown,
)
from ..engine.backtest import trades_parquet as encode_trades
from ..engine.portfolio.lifecycle import DEFAULT_DISCOUNT, BacktestFacts, discounted_benchmark
from ..engine.strategy.config import parse_config
from ..errors import HatError
from ..models import (
    HatBacktestRun,
    HatBot,
    HatPosition,
    HatStrategyVersion,
    HatTradeResult,
    HatUniverseVersion,
)
from ..risk.policy import PolicyVersion, TradeOutcome, build_gate_evidence, effective_version
from ..risk.policy_service import load_versions
from ..time import as_utc, utc_now
from .costs import (
    DEFAULT_COST_MODEL,
    DEFAULT_SLIPPAGE_BPS,
    BacktestFills,
    CostError,
    check_slippage,
    load_cost_model,
)
from .dsr import (
    MIN_TRIALS,
    PBO_BLOCKS,
    VARIANCE_FLOOR,
    Deflated,
    ReturnStats,
    family_deflated_sharpe,
    pbo,
    trial_variance,
)
from .fills import AMBIGUOUS_OPTIMISTIC, PENETRATION, SUSPENSION_SESSIONS
from .market import load_market
from .slots import SLOT_ORDER_TIME

__all__ = [
    "COMPLETED",
    "FAILED",
    "RUNNING",
    "RunConventions",
    "RunError",
    "backtest_bot",
    "backtest_evidence",
    "RETURNS_SCHEMA",
    "complete_run",
    "fail_run",
    "family_key",
    "family_pbo",
    "family_sharpes",
    "gate_evidence",
    "read_returns",
    "record_backtest",
    "returns_path",
    "run_dsr",
    "start_run",
    "trades_path",
    "trial_count",
]

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
_DSR_PLACES = Decimal("0.000001")


class RunError(HatError):
    """A run could not be recorded; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def family_key(strategy_id: uuid.UUID, universe_id: uuid.UUID) -> str:
    """The trial family of a strategy on a universe: equal for two runs iff they share both."""
    return f"{uuid.UUID(str(strategy_id))}:{uuid.UUID(str(universe_id))}"


def trades_path(root: Path, run_id: uuid.UUID) -> Path:
    """`backtests/run_id=<id>/trades.parquet` under the HAT data root (HAT-BT-032)."""
    return root / "backtests" / f"run_id={run_id}" / "trades.parquet"


def returns_path(root: Path, run_id: uuid.UUID) -> Path:
    """`backtests/run_id=<id>/returns.parquet`: the run's daily returns (PR 66)."""
    return root / "backtests" / f"run_id={run_id}" / "returns.parquet"


RETURNS_SCHEMA = pa.schema(
    [("date", pa.date32()), ("equity", pa.decimal128(18, 4)), ("ret", pa.float64())]
)


def _returns_parquet(result: BacktestResult) -> bytes:
    """The run's session closes and daily returns, each against the close before (the first
    against `metrics.initial_equity`; null without it, or after a close at or below 0): the
    series `metrics.returns` describes."""
    raw = result.metrics.get("initial_equity")
    previous = None if raw is None else float(Decimal(str(raw)))
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
    """A run's daily returns by date; None when it has no returns file (a run before PR 66) or
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


def trial_count(session: Session, family: str) -> int:
    """The family's trials: every run recorded for it, whatever its status (HAT-BT-030)."""
    count = session.scalar(
        select(func.count()).select_from(HatBacktestRun).where(HatBacktestRun.family_key == family)
    )
    return int(count or 0)


def _stats(metrics: Mapping[str, Any] | None) -> ReturnStats | None:
    raw = (metrics or {}).get("returns")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ReturnStats(
            sr=float(raw["sr"]),
            t=int(raw["t"]),
            skew=float(raw["skew"]),
            kurtosis=float(raw["kurtosis"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _completed(session: Session, family: str) -> list[HatBacktestRun]:
    return list(
        session.scalars(
            select(HatBacktestRun)
            .where(HatBacktestRun.family_key == family, HatBacktestRun.status == COMPLETED)
            .order_by(HatBacktestRun.id)
        ).all()
    )


def family_sharpes(session: Session, family: str) -> list[float]:
    """The per-period Sharpe ratios of the family's completed runs, oldest first. A run that
    failed or whose returns had no Sharpe ratio counts as a trial but adds no variance."""
    return [s.sr for s in (_stats(r.metrics) for r in _completed(session, family)) if s is not None]


def _recorded(session: Session, run: HatBacktestRun) -> HatBacktestRun:
    """`run` when it is a recorded `hat_backtest_run` row; refused otherwise (PR 66, G33)."""
    if run.id is None or session.get(HatBacktestRun, run.id) is not run:
        raise RunError("backtest_run_unrecorded")
    return run


def run_dsr(
    session: Session, run: HatBacktestRun, metrics: Mapping[str, Any] | None = None
) -> tuple[Deflated, int, float]:
    """A recorded run's deflated Sharpe probability now, with the family's trial count and the
    variance of its Sharpe ratios (the run's own included, completed or not), through the
    family floors (`family_deflated_sharpe`); `metrics` stands for the run's own while it is
    being completed. A run that is not recorded is refused (`backtest_run_unrecorded`): only a
    counted trial is scored."""
    _recorded(session, run)
    stats = _stats(run.metrics if metrics is None else metrics)
    # one read of the family, so the trial count and the Sharpe ratios are of the same moment
    family = session.scalars(
        select(HatBacktestRun).where(HatBacktestRun.family_key == run.family_key)
    ).all()
    sharpes = [
        s.sr
        for s in (_stats(r.metrics) for r in family if r.status == COMPLETED and r.id != run.id)
        if s is not None
    ]
    if stats is not None:
        sharpes.append(stats.sr)
    trials = len(family)
    variance = trial_variance(sharpes)
    return family_deflated_sharpe(stats, trials, variance), trials, variance


def _dsr_json(deflated: Deflated, trials: int, variance: float) -> dict[str, Any]:
    return {
        "prob": str(Decimal(repr(deflated.prob)).quantize(_DSR_PLACES)),
        "sr0": deflated.sr0,
        "reason": deflated.reason,
        "trials": trials,
        "variance": variance,
        # the floors the probability was read through (PR 66, N15)
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
        "prob": str(Decimal(repr(found.prob)).quantize(_DSR_PLACES)),
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
        raise RunError("backtest_unknown_version")
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
    if _recorded(session, run).status != RUNNING:
        raise RunError("backtest_run_not_running")
    atomic_replace(
        trades_path(root, run.id), lambda tmp: tmp.write_bytes(encode_trades(result.trades))
    )
    atomic_replace(
        returns_path(root, run.id), lambda tmp: tmp.write_bytes(_returns_parquet(result))
    )
    # everything is computed before the one change of status, so nothing can fail in between
    deflated, trials, variance = run_dsr(session, run, result.metrics)
    metrics = {
        **result.metrics,
        "dsr": _dsr_json(deflated, trials, variance),
        "pbo": family_pbo(session, run.family_key, root, also=run),
        "completed_at": utc_now().isoformat(),
    }
    run.metrics = metrics
    run.status = COMPLETED
    session.flush()
    return run


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
    (still a trial) and its code raised as `RunError`; any other error leaves the run `failed`
    and raises `RunError("backtest_error")` chained to it. A caller that commits after catching
    `RunError` keeps the trial (the command line does)."""
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
        raise RunError(exc.code) from None
    except Exception as exc:
        # Whatever went wrong, the run stays a trial of its family (HAT-BT-030).
        fail_run(session, run, "backtest_error")
        raise RunError("backtest_error") from exc


@dataclass(frozen=True)
class RunConventions:
    """The rules a run chooses besides its slippage and cost model (`RunSpec`): the entry fill
    (PR 62), the slot order (PR 63), the ambiguous-day rule and the bar-less sessions before the
    suspension markdown (PR 64), the sleeve weight (None: the policy's base weight) and the
    regime rule the entries are sized under (PR 68)."""

    entry_fill_rule: str = PENETRATION
    slot_order: str = SLOT_ORDER_TIME
    ambiguous: str = AMBIGUOUS_OPTIMISTIC
    suspension_sessions: int = SUSPENSION_SESSIONS
    sleeve_weight: Decimal | None = None
    regime_mult: str = REGIME_MULT_ONE


def backtest_bot(
    session: Session,
    store: DataStore,
    bot_id: uuid.UUID,
    start: date,
    end: date,
    *,
    root: Path,
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
    cost_model: str = DEFAULT_COST_MODEL,
    version_id: uuid.UUID | None = None,
    cooldown_sessions: int | None = None,
    for_promotion: bool = False,
    conventions: RunConventions | None = None,
) -> HatBacktestRun:
    """Backtest a sleeve's strategy version (its live one unless `version_id` names another of
    the same strategy) on the sleeve's universe under the risk policy in force now, over the
    Parquet store; `cooldown_sessions` replaces that policy's cooldown for a sweep (PR 58), and
    `conventions` the default entry fill (PR 62), slot order (PR 63), ambiguous-day rule and
    bar-less sessions before the markdown (PR 64), sleeve weight and regime rule (PR 68); the
    `policy` regime rule also reads the regime engine's index, VIX and breadth. A run
    `for_promotion` (one `publish_version` may take) is refused at a slippage of 0, under
    `touch`, off the `time` slot order or with a markdown later than the default. The command line's
    body.
    Refusals before the run is recorded raise `RunError` and record nothing; a refusal of the
    engine is a failed trial."""
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise RunError("backtest_unknown_bot")
    version = session.get(HatStrategyVersion, version_id or bot.live_version_id)
    if version is None or version.strategy_id != bot.strategy_id:
        raise RunError("backtest_unknown_version")
    universe_id = _universe_of(session, bot)
    if universe_id is None:
        raise RunError("backtest_no_universe")
    policy = effective_version(load_versions(session, bot.user_id), utc_now())
    if policy is None:
        raise RunError("backtest_no_policy")
    if cooldown_sessions is not None:
        try:
            policy = with_cooldown(policy, cooldown_sessions)
        except BacktestError as exc:
            raise RunError(exc.code) from None
    config = parse_config(version.config)
    rules = conventions or RunConventions()
    try:
        fills = BacktestFills(load_cost_model(cost_model), check_slippage(slippage_bps))
    except CostError as exc:
        raise RunError(exc.code) from None
    try:
        check_conventions(
            rules.entry_fill_rule,
            fills.slippage_bps,
            for_promotion,
            rules.slot_order,
            ambiguous=rules.ambiguous,
            suspension_sessions=rules.suspension_sessions,
            sleeve_weight=rules.sleeve_weight,
            regime_mult=rules.regime_mult,
        )
    except BacktestError as exc:
        raise RunError(exc.code) from None
    spec = RunSpec(
        config=config,
        strategy_version_id=version.id,
        policy=policy,
        start=start,
        end=end,
        fills=fills,
        entry_fill_rule=rules.entry_fill_rule,
        for_promotion=for_promotion,
        slot_order=rules.slot_order,
        ambiguous=rules.ambiguous,
        suspension_sessions=rules.suspension_sessions,
        sleeve_weight=rules.sleeve_weight,
        regime_mult=rules.regime_mult,
    )
    try:
        sleeve_weight(spec)  # before the store is read
    except BacktestError as exc:
        raise RunError(exc.code) from None
    try:
        data, snapshot = load_market(
            store,
            session,
            universe_id=universe_id,
            index=config.regime.index,
            start=start,
            end=end,
            regime_inputs=rules.regime_mult == REGIME_MULT_POLICY,
        )
    except BacktestError as exc:
        raise RunError(exc.code) from None
    return record_backtest(
        session, spec, data, universe_id=universe_id, data_snapshot=snapshot, root=root
    )


# --- evidence ---------------------------------------------------------------------------------


def _universe_of(session: Session, bot: HatBot) -> uuid.UUID | None:
    if bot.universe_version_id is None:
        return None
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return None if version is None else version.universe_id


def _cooldown_of(policy: PolicyVersion | None) -> int:
    """The `cooldown_sessions` of a policy version (0 without one)."""
    return 0 if policy is None else policy.policy.cooldown_sessions


def _completion(run: HatBacktestRun) -> tuple[datetime, uuid.UUID]:
    """When a run completed (`metrics.completed_at`, PR 66), else when it started (its uuid7
    id's milliseconds, for a run recorded before PR 66); the id breaks a tie."""
    raw = (run.metrics or {}).get("completed_at")
    try:
        at = as_utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else None
    except ValueError:
        at = None
    if at is None:
        at = datetime.fromtimestamp((run.id.int >> 80) / 1000, tz=UTC)
    return at, run.id


def _latest_run(session: Session, bot: HatBot, cooldown: int) -> HatBacktestRun | None:
    """The newest completed run (by completion time, PR 66) of the bot's live version on the
    bot's universe that ran under `cooldown`, the one in force when the evidence is read
    (HAT-RISK-036): a
    `--cooldown-sessions` sweep run, or one made before the cooldown changed, traded another rule
    and is not the sleeve's evidence. A run recorded before the field existed ran with none (0).
    Only a run flagged for promotion counts (PR 62, O5): a sensitivity run (`touch`, 0 bps) or
    one recorded before the flag existed is never the evidence; nor is one whose slots went in
    another order than the estimated trigger time (PR 63), as every run before PR 63 did, nor
    one recorded before the path rules (PR 64), which sold at locked lower bands and carried
    suspended positions at their last close, nor one recorded before the sleeve's allocation
    sized it (PR 68), which sized off the whole account; a promotion run is sized at the base
    weight and regime rule `1` (`check_conventions`)."""
    universe_id = _universe_of(session, bot)
    if bot.live_version_id is None or universe_id is None:
        return None
    candidates = sorted(
        session.scalars(
            select(HatBacktestRun).where(
                HatBacktestRun.strategy_version_id == bot.live_version_id,
                HatBacktestRun.family_key == family_key(bot.strategy_id, universe_id),
                HatBacktestRun.status == COMPLETED,
            )
        ),
        key=_completion,
        reverse=True,
    )
    return next(
        (
            run
            for run in candidates
            if (run.metrics or {}).get("cooldown_sessions", 0) == cooldown
            and (run.metrics or {}).get("for_promotion") is True
            and (run.metrics or {}).get("slot_order") == SLOT_ORDER_TIME
            and (run.metrics or {}).get("lower_circuit") == LOWER_CIRCUIT_RULE
            and (run.metrics or {}).get("regime_mult") == REGIME_MULT_ONE
        ),
        None,
    )


def _expectancy(metrics: Mapping[str, Any]) -> Decimal | None:
    raw = metrics.get("expectancy_r")
    return None if raw is None else Decimal(str(raw))


def backtest_evidence(session: Session, bot: HatBot) -> BacktestFacts:
    """The sleeve's backtest evidence (HAT-BT-060): the newest completed run of its live version
    on its universe, with the DSR recomputed at the family's current trial count and Sharpe
    variance. No run gives no evidence, so Research -> Backtested stays closed."""
    in_force = effective_version(load_versions(session, bot.user_id), utc_now())
    run = _latest_run(session, bot, _cooldown_of(in_force))
    if run is None:
        return BacktestFacts()
    metrics = run.metrics or {}
    deflated, trials, _ = run_dsr(session, run)
    trades = metrics.get("trades")
    return BacktestFacts(
        dsr_prob=Decimal(repr(deflated.prob)).quantize(_DSR_PLACES),
        trades=trades if isinstance(trades, int) and not isinstance(trades, bool) else 0,
        expectancy=_expectancy(metrics),
        trials=trials,
    )


def gate_evidence(
    session: Session, user_id: uuid.UUID, at: datetime, discount: Decimal = DEFAULT_DISCOUNT
) -> dict[str, Any] | None:
    """The `gate_evidence` a risk increase requested at `at` is checked with (HAT-GOV-091): the
    trades of the user's sleeves closed since the policy version in force at `at` took effect
    (every closed trade when none is in force; shadow ones are ignored by
    `build_gate_evidence`), so each step up needs its own evidence, against the discounted
    backtest expectancy, the trade-weighted mean over the sleeves' newest completed runs. None
    when no sleeve has a completed run: the increase gate then refuses."""
    bots = session.scalars(
        select(HatBot).where(HatBot.user_id == user_id).order_by(HatBot.id)
    ).all()
    in_force = effective_version(load_versions(session, user_id), at)
    weighted = Decimal(0)
    count = 0
    for bot in bots:
        run = _latest_run(session, bot, _cooldown_of(in_force))
        if run is None:
            continue
        expectancy = _expectancy(run.metrics or {})
        trades = (run.metrics or {}).get("trades")
        if expectancy is None or not isinstance(trades, int) or trades <= 0:
            continue
        weighted += expectancy * trades
        count += trades
    if count == 0:
        return None
    query = (
        select(HatTradeResult.r_multiple, HatPosition.is_virtual)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .join(HatBot, HatBot.id == HatPosition.bot_id)
        .where(HatBot.user_id == user_id)
        .order_by(HatPosition.closed_at, HatPosition.id)
    )
    if in_force is not None:
        query = query.where(HatPosition.closed_at >= as_utc(in_force.effective_from))
    rows = session.execute(query).all()
    outcomes = [TradeOutcome(Decimal(r), bool(virtual)) for r, virtual in rows]
    return build_gate_evidence(outcomes, discounted_benchmark(weighted / count, discount))
