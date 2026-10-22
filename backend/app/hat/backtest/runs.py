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
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.store import DataStore, atomic_replace
from ..engine.backtest import (
    BacktestError,
    BacktestResult,
    MarketData,
    RunSpec,
    check_conventions,
    run_backtest,
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
from .dsr import Deflated, ReturnStats, deflated_sharpe, trial_variance
from .fills import PENETRATION
from .market import load_market

__all__ = [
    "COMPLETED",
    "FAILED",
    "RUNNING",
    "RunError",
    "backtest_bot",
    "backtest_evidence",
    "complete_run",
    "deflate",
    "fail_run",
    "family_key",
    "family_sharpes",
    "gate_evidence",
    "record_backtest",
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


def family_sharpes(session: Session, family: str) -> list[float]:
    """The per-period Sharpe ratios of the family's completed runs, oldest first. A run that
    failed or whose returns had no Sharpe ratio counts as a trial but adds no variance."""
    runs = session.scalars(
        select(HatBacktestRun)
        .where(HatBacktestRun.family_key == family, HatBacktestRun.status == COMPLETED)
        .order_by(HatBacktestRun.id)
    ).all()
    return [s.sr for s in (_stats(r.metrics) for r in runs) if s is not None]


def deflate(stats: ReturnStats | None, trials: int, sharpes: Sequence[float]) -> Deflated:
    """A run's deflated Sharpe probability against its family's trials and Sharpe ratios."""
    return deflated_sharpe(stats, max(1, trials), trial_variance(sharpes))


def _dsr_json(deflated: Deflated, trials: int, variance: float) -> dict[str, Any]:
    return {
        "prob": str(Decimal(repr(deflated.prob)).quantize(_DSR_PLACES)),
        "sr0": deflated.sr0,
        "reason": deflated.reason,
        "trials": trials,
        "variance": variance,
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
    """Write the run's `trades.parquet` and store its metrics with the DSR at this moment's
    family trial count and variance."""
    if run.status != RUNNING:
        raise RunError("backtest_run_not_running")
    atomic_replace(
        trades_path(root, run.id), lambda tmp: tmp.write_bytes(encode_trades(result.trades))
    )
    trials = trial_count(session, run.family_key)
    stats = _stats(result.metrics)
    sharpes = family_sharpes(session, run.family_key)
    if stats is not None:
        sharpes = [*sharpes, stats.sr]
    deflated = deflate(stats, trials, sharpes)
    run.metrics = {**result.metrics, "dsr": _dsr_json(deflated, trials, trial_variance(sharpes))}
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
    entry_fill_rule: str = PENETRATION,
    for_promotion: bool = False,
) -> HatBacktestRun:
    """Backtest a sleeve's strategy version (its live one unless `version_id` names another of
    the same strategy) on the sleeve's universe under the risk policy in force now, over the
    Parquet store; `cooldown_sessions` replaces that policy's cooldown for a sweep (PR 58), and
    `entry_fill_rule` the default penetration fill (PR 62). A run `for_promotion` (one
    `publish_version` may take) is refused at a slippage of 0 or under `touch`. The command
    line's body.
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
    try:
        fills = BacktestFills(load_cost_model(cost_model), check_slippage(slippage_bps))
    except CostError as exc:
        raise RunError(exc.code) from None
    try:
        check_conventions(entry_fill_rule, fills.slippage_bps, for_promotion)
    except BacktestError as exc:
        raise RunError(exc.code) from None
    try:
        data, snapshot = load_market(
            store, session, universe_id=universe_id, index=config.regime.index, start=start, end=end
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
        entry_fill_rule=entry_fill_rule,
        for_promotion=for_promotion,
    )
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


def _latest_run(session: Session, bot: HatBot, cooldown: int) -> HatBacktestRun | None:
    """The newest completed run of the bot's live version on the bot's universe that ran under
    `cooldown`, the one in force when the evidence is read (HAT-RISK-036): a
    `--cooldown-sessions` sweep run, or one made before the cooldown changed, traded another rule
    and is not the sleeve's evidence. A run recorded before the field existed ran with none (0).
    Only a run flagged for promotion counts (PR 62, O5): a sensitivity run (`touch`, 0 bps) or
    one recorded before the flag existed is never the evidence."""
    universe_id = _universe_of(session, bot)
    if bot.live_version_id is None or universe_id is None:
        return None
    candidates = session.scalars(
        select(HatBacktestRun)
        .where(
            HatBacktestRun.strategy_version_id == bot.live_version_id,
            HatBacktestRun.family_key == family_key(bot.strategy_id, universe_id),
            HatBacktestRun.status == COMPLETED,
        )
        .order_by(HatBacktestRun.id.desc())
    )
    return next(
        (
            run
            for run in candidates
            if (run.metrics or {}).get("cooldown_sessions", 0) == cooldown
            and (run.metrics or {}).get("for_promotion") is True
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
    trials = trial_count(session, run.family_key)
    deflated = deflate(_stats(metrics), trials, family_sharpes(session, run.family_key))
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
