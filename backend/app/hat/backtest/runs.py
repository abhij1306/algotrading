"""Backtest runs, trial counting and the evidence they give.

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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.store import DataStore, atomic_replace
from ..engine.backtest import (
    END_OF_PERIOD,
    GAP_PLAYBOOKS,
    KNOWN_GAPS,
    LOWER_CIRCUIT_RULE,
    REGIME_MULT_POLICY,
    BacktestError,
    BacktestResult,
    MarketData,
    RunConventions,
    RunSpec,
    run_backtest,
    with_cooldown,
)
from ..engine.backtest import trades_parquet as encode_trades
from ..engine.confidence import (
    NO_POLICY,
    NO_RUN,
    NO_SCORE,
    NOT_BACKTESTED,
    BucketStats,
    Confidence,
    band_of,
    buckets,
    confidence_of,
)
from ..engine.portfolio.allocator import sleeve_key
from ..engine.portfolio.lifecycle import DEFAULT_DISCOUNT, BacktestFacts, discounted_benchmark
from ..engine.strategy.config import ConfigError, StrategyConfig, parse_config
from ..models import (
    HatBacktestBucket,
    HatBacktestRun,
    HatBot,
    HatPosition,
    HatSignal,
    HatStrategyVersion,
    HatTradeResult,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..risk.policy import (
    PolicyError,
    PolicyVersion,
    TradeOutcome,
    build_gate_evidence,
    effective_version,
)
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
from .market import load_market
from .slots import SLOT_ORDER_TIME

__all__ = [
    "COMPLETED",
    "END_OF_PERIOD",
    "FAILED",
    "RUNNING",
    "RunRequest",
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
    "signal_confidence",
    "start_run",
    "trades_path",
    "trial_count",
    "unbacktested",
    "universe_of",
]

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
_DSR_PLACES = Decimal("0.000001")


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
        raise BacktestError("backtest_run_unrecorded")
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
    if _recorded(session, run).status != RUNNING:
        raise BacktestError("backtest_run_not_running")
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
    session.add_all(bucket_rows(run.id, result))
    session.flush()
    return run


def bucket_rows(run_id: uuid.UUID, result: BacktestResult) -> list[HatBacktestBucket]:
    """The run's confidence buckets (D132, PR 71): its closed trades by playbook and setup-score
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
    model), the cooldown replacing the policy's for a sweep (PR 58), whether the run may
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
    index, VIX and breadth. The command line's body. Refusals before the run is recorded (an
    unknown bot or version, conventions a promotion run may not use, ...) raise `BacktestError`
    and record nothing; a refusal of the engine is a failed trial."""
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


# --- evidence ---------------------------------------------------------------------------------


def universe_of(session: Session, bot: HatBot) -> uuid.UUID | None:
    """The universe of the bot's pinned universe version (None without one)."""
    if bot.universe_version_id is None:
        return None
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return None if version is None else version.universe_id


def _cooldown_of(policy: PolicyVersion | None) -> int:
    """The `cooldown_sessions` of a policy version (0 without one)."""
    return 0 if policy is None else policy.policy.cooldown_sessions


def _metrics(run: HatBacktestRun) -> Mapping[str, Any]:
    """A run's metrics; anything but an object reads as none, so every fact is absent."""
    return run.metrics if isinstance(run.metrics, dict) else {}


def _completion(run: HatBacktestRun) -> tuple[datetime, uuid.UUID]:
    """When a run completed (`metrics.completed_at`, PR 66), else when it started (its uuid7
    id's milliseconds, for a run recorded before PR 66); the id breaks a tie."""
    at = _completed_at(run)
    if at is None:
        at = datetime.fromtimestamp((run.id.int >> 80) / 1000, tz=UTC)
    return at, run.id


def _completed_at(run: HatBacktestRun) -> datetime | None:
    """`metrics.completed_at` (PR 66), None when missing or unreadable."""
    raw = _metrics(run).get("completed_at")
    try:
        return as_utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else None
    except ValueError:
        return None


def _latest_run(
    session: Session, bot: HatBot, cooldown: int, at: datetime | None = None
) -> HatBacktestRun | None:
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
    sized it (PR 68), which sized off the whole account, nor one sized under regime rule `1`
    (PR 69): a promotion run is sized at the base weight under the live regime rule `policy`
    (`RunConventions.check_promotion`). Given `at`, only a run with a readable completion time
    at or before it is the evidence (PR 71): a signal processed late never reads results that
    did not exist when it triggered."""
    universe_id = universe_of(session, bot)
    if bot.live_version_id is None or universe_id is None:
        return None
    cutoff = None if at is None else as_utc(at)
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
            if (cutoff is None or ((done := _completed_at(run)) is not None and done <= cutoff))
            and _metrics(run).get("cooldown_sessions", 0) == cooldown
            and _metrics(run).get("for_promotion") is True
            and _metrics(run).get("slot_order") == SLOT_ORDER_TIME
            and _metrics(run).get("lower_circuit") == LOWER_CIRCUIT_RULE
            and _metrics(run).get("regime_mult") == REGIME_MULT_POLICY
        ),
        None,
    )


def _expectancy(metrics: Mapping[str, Any]) -> Decimal | None:
    raw = metrics.get("expectancy_r")
    return None if raw is None else Decimal(str(raw))


def _config_of(version: HatStrategyVersion | None) -> StrategyConfig | None:
    """The version's config, None when it has none that parses (its playbooks are then
    unknown, and the gate fails closed)."""
    if version is None:
        return None
    try:
        return parse_config(version.config)
    except ConfigError:
        return None


def _count(metrics: Mapping[str, Any], key: str) -> int | None:
    raw = metrics.get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _day(metrics: Mapping[str, Any], key: str) -> date | None:
    raw = metrics.get(key)
    try:
        return date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _weight(raw: object) -> Decimal | None:
    try:
        return Decimal(raw) if isinstance(raw, str) else None
    except InvalidOperation:
        return None


def _gapped(metrics: Mapping[str, Any], playbooks: frozenset[str]) -> frozenset[str]:
    """The playbooks the run's known gaps name (`GAP_PLAYBOOKS`); a run without a readable gap
    list may have any gap, so every one of `playbooks` counts as named."""
    gaps = metrics.get("known_gaps")
    if isinstance(gaps, list) and all(isinstance(code, str) for code in gaps):
        return frozenset(p for code in gaps for p in GAP_PLAYBOOKS.get(code, ()))
    return playbooks


# The playbooks every daily-bar run's known gaps name (EP until PR 70).
DAILY_BAR_GAPPED = frozenset(p for code in KNOWN_GAPS for p in GAP_PLAYBOOKS.get(code, ()))


def unbacktested(metrics: Mapping[str, Any] | None) -> frozenset[str]:
    """The playbooks whose live and shadow trades are left out when they are compared with a run
    (PR 40): those its known gaps name, as a run cannot have their trades; with no run or no
    readable gap list, those every daily-bar run's gaps name (`DAILY_BAR_GAPPED`). EP drops out
    once a run no longer names it (PR 70)."""
    return DAILY_BAR_GAPPED if metrics is None else _gapped(metrics, DAILY_BAR_GAPPED)


def backtest_evidence(session: Session, bot: HatBot) -> BacktestFacts:
    """The sleeve's backtest evidence (HAT-BT-060): the newest completed run of its live version
    on its universe, with the DSR recomputed at the family's current trial count and Sharpe
    variance; since PR 69 also what the rest of the gate reads (HAT-PORT-061): the run's first
    and last session, ambiguous days and held sessions, the playbooks its known gaps name
    (`GAP_PLAYBOOKS`) and those the version enables, and its sleeve weight and regime rule
    beside the base weight of the sleeve in the policy in force. A fact the run did not record
    is absent, and fails its condition. No run gives no evidence, so Research -> Backtested
    stays closed. The playbooks `unbacktested` names are not compared with it (PR 40)."""
    in_force = effective_version(load_versions(session, bot.user_id), utc_now())
    run = _latest_run(session, bot, _cooldown_of(in_force))
    if run is None:
        return BacktestFacts(unbacktested=unbacktested(None))
    metrics = _metrics(run)
    deflated, trials, _ = run_dsr(session, run)
    config = _config_of(session.get(HatStrategyVersion, run.strategy_version_id))
    playbooks = frozenset(() if config is None else (p.name for p in config.playbooks))
    gapped = _gapped(metrics, playbooks)
    base = (
        None
        if in_force is None or config is None
        else in_force.policy.base_weight.get(sleeve_key(config.bot))
    )
    return BacktestFacts(
        dsr_prob=Decimal(repr(deflated.prob)).quantize(_DSR_PLACES),
        trades=_count(metrics, "trades") or 0,
        expectancy=_expectancy(metrics),
        trials=trials,
        first_session=_day(metrics, "first_session"),
        last_session=_day(metrics, "last_session"),
        ambiguous_days=_count(metrics, "ambiguous_days"),
        held_sessions=_count(metrics, "held_sessions") or 0,
        gapped_playbooks=gapped,
        playbooks=playbooks,
        sleeve_weight=_weight(metrics.get("sleeve_weight")),
        base_weight=base,
        regime_mult=metrics.get("regime_mult"),
        unbacktested=unbacktested(metrics),
    )


def signal_confidence(
    session: Session,
    bot: HatBot,
    playbook: str,
    instrument_id: uuid.UUID,
    trade_date: date,
    at: datetime,
) -> Confidence:
    """The confidence label of a signal of `bot` (D132, HAT-EXEC-015): the bucket of its playbook
    and its watchlist row's setup-score band in the run `backtest_evidence` reads, among those
    completed by `at`. Low when there is no such run, when the run's known gaps name the
    playbook (EP until it is backtested on 5-minute bars, PR 70), when the signal has no single
    score in a band, or when the bucket is empty; otherwise `confidence.label_of` decides. An
    unreadable policy history gives low too: the run under the cooldown in force cannot be
    told."""
    try:
        in_force = effective_version(load_versions(session, bot.user_id), at)
    except PolicyError:
        return Confidence.low(NO_POLICY, playbook=playbook)
    run = _latest_run(session, bot, _cooldown_of(in_force), at)
    if run is None:
        return Confidence.low(NO_RUN, playbook=playbook)
    run_id = str(run.id)
    config = _config_of(session.get(HatStrategyVersion, run.strategy_version_id))
    playbooks = frozenset(() if config is None else (p.name for p in config.playbooks))
    # an unreadable gap list names every playbook, the signal's own included
    if config is None or playbook in _gapped(_metrics(run), playbooks | {playbook}):
        return Confidence.low(NOT_BACKTESTED, run_id=run_id, playbook=playbook)
    scores = set(
        session.scalars(
            select(HatWatchlistEntry.score).where(
                HatWatchlistEntry.bot_id == bot.id,
                HatWatchlistEntry.strategy_version_id == run.strategy_version_id,
                HatWatchlistEntry.playbook == playbook,
                HatWatchlistEntry.instrument_id == instrument_id,
                HatWatchlistEntry.trade_date == trade_date,
            )
        )
    )
    bands = {band_of(score) for score in scores}
    band = next(iter(bands)) if len(bands) == 1 else None
    if band is None:
        # no watchlist row, a score outside the bands, or two rows in different bands
        return Confidence.low(NO_SCORE, run_id=run_id, playbook=playbook)
    row = session.get(HatBacktestBucket, (run.id, playbook, band))
    stats = (
        None
        if row is None
        else BucketStats(row.trades, row.mean_r, row.ci90_low, row.ci90_high, row.win_rate)
    )
    score = min(scores)
    return confidence_of(stats, run_id=run_id, playbook=playbook, band=band, score=score)


def gate_evidence(
    session: Session, user_id: uuid.UUID, at: datetime, discount: Decimal = DEFAULT_DISCOUNT
) -> dict[str, Any] | None:
    """The `gate_evidence` a risk increase requested at `at` is checked with (HAT-GOV-091): the
    trades of the user's sleeves closed since the policy version in force at `at` took effect
    (every closed trade when none is in force; shadow ones are ignored by
    `build_gate_evidence`), so each step up needs its own evidence, against the discounted
    backtest expectancy, the trade-weighted mean over the sleeves' newest completed runs. None
    when no sleeve has a completed run: the increase gate then refuses. A sleeve's trades of a
    playbook its run cannot have (`unbacktested`: EP until PR 70) are not counted (PR 40)."""
    bots = session.scalars(
        select(HatBot).where(HatBot.user_id == user_id).order_by(HatBot.id)
    ).all()
    in_force = effective_version(load_versions(session, user_id), at)
    weighted = Decimal(0)
    count = 0
    excluded: dict[uuid.UUID, frozenset[str]] = {}
    for bot in bots:
        run = _latest_run(session, bot, _cooldown_of(in_force))
        metrics = None if run is None else _metrics(run)
        excluded[bot.id] = unbacktested(metrics)
        if metrics is None:
            continue
        expectancy = _expectancy(metrics)
        trades = metrics.get("trades")
        if expectancy is None or not isinstance(trades, int) or trades <= 0:
            continue
        weighted += expectancy * trades
        count += trades
    if count == 0:
        return None
    query = (
        select(
            HatTradeResult.r_multiple,
            HatPosition.is_virtual,
            HatPosition.bot_id,
            HatSignal.playbook,
        )
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .join(HatBot, HatBot.id == HatPosition.bot_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatBot.user_id == user_id)
        .order_by(HatPosition.closed_at, HatPosition.id)
    )
    if in_force is not None:
        query = query.where(HatPosition.closed_at >= as_utc(in_force.effective_from))
    rows = session.execute(query).all()
    outcomes = [
        TradeOutcome(Decimal(r), bool(virtual))
        for r, virtual, bot_id, playbook in rows
        if playbook not in excluded.get(bot_id, frozenset())
    ]
    return build_gate_evidence(outcomes, discounted_benchmark(weighted / count, discount))
