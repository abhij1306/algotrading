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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from ..engine.portfolio.regime import INDEX_NAME
from ..engine.portfolio.regime_service import regime_profile, stored_regime_table
from ..engine.strategy.config import StrategyConfig, parse_config
from ..models import (
    HatBacktestBucket,
    HatBacktestRun,
    HatBot,
    HatStrategy,
    HatStrategyVersion,
    HatUniverse,
    HatUniverseVersion,
)
from ..money import exact, require_exact
from ..risk.policy import PolicyVersion, effective_version
from ..risk.policy_upkeep import load_versions
from ..time import utc_now
from .data import MarketData, load_market
from .dsr import MIN_TRIALS, PBO_BLOCKS, VARIANCE_FLOOR, Deflated, pbo
from .evidence import COMPLETED, DSR_DIGITS, RunMetrics, family_key, run_dsr, universe_of
from .loop import run_backtest
from .overrides import OverrideError, Overrides, parse_set
from .report import BacktestResult
from .report import trades_parquet as encode_trades
from .report_data import build_report, write_report
from .spec import REGIME_MULT_POLICY, BacktestError, RunConventions, RunSpec, with_cooldown

__all__ = [
    "FAILED",
    "RETURNS_SCHEMA",
    "RUNNING",
    "RunRequest",
    "SweepOutcome",
    "Variant",
    "backtest_bot",
    "complete_run",
    "fail_run",
    "family_pbo",
    "parse_variants",
    "read_returns",
    "record_backtest",
    "returns_path",
    "start_run",
    "sweep_bot",
    "trades_path",
]

RUNNING = "running"
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
        "prob": str(require_exact(deflated.prob, "dsr_prob", floats=True, places=DSR_DIGITS)),
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
        "prob": str(require_exact(found.prob, "pbo_prob", floats=True, places=DSR_DIGITS)),
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
    session: Session,
    run: HatBacktestRun,
    result: BacktestResult,
    *,
    root: Path,
    benchmark: Sequence[tuple[date, Decimal]] = (),
    variant: Mapping[str, str] | None = None,
) -> HatBacktestRun:
    """Write the run's `trades.parquet`, `returns.parquet` and `report.json` (`report_data`,
    charted against the `benchmark` NIFTY 500 closes) and store its metrics with the DSR at this
    moment's family trial count and variance, the family's PBO with this run among its variants,
    the completion time and, for a sweep's run, its `variant` (label and sweep id)."""
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
    initial = RunMetrics.from_json(result.metrics).initial_equity
    write_report(root, run.id, build_report(result, initial or Decimal(0), benchmark))
    # everything is computed before the one change of status, so nothing can fail in between
    deflated, trials, variance = run_dsr(session, run, RunMetrics.from_json(result.metrics))
    metrics = {
        **result.metrics,
        "dsr": _dsr_json(deflated, trials, variance),
        "pbo": family_pbo(session, run.family_key, root, also=run),
        "completed_at": utc_now().isoformat(),
        **({} if variant is None else {"variant": dict(variant)}),
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


def fail_run(
    session: Session, run: HatBacktestRun, code: str, variant: Mapping[str, str] | None = None
) -> HatBacktestRun:
    """End a run as `failed` with a fixed error code (and a sweep's `variant`); it stays a trial
    of its family."""
    run.status = FAILED
    run.metrics = {"error": code, **({} if variant is None else {"variant": dict(variant)})}
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
    variant: Mapping[str, str] | None = None,
) -> HatBacktestRun:
    """Record, run and complete one backtest (a sweep's run with its `variant`). A run the
    engine refuses is stored as `failed` (still a trial) and its `BacktestError` raised; any
    other error leaves the run `failed` and raises `BacktestError("backtest_error")` chained to
    it. A caller that commits after catching `BacktestError` keeps the trial (the command line
    does)."""
    run, error = _record(
        session,
        spec,
        data,
        universe_id=universe_id,
        data_snapshot=data_snapshot,
        root=root,
        variant=variant,
    )
    if error is not None:
        raise error
    return run


def _record(
    session: Session,
    spec: RunSpec,
    data: MarketData,
    *,
    universe_id: uuid.UUID,
    data_snapshot: Mapping[str, Any],
    root: Path,
    variant: Mapping[str, str] | None,
) -> tuple[HatBacktestRun, BacktestError | None]:
    """`record_backtest`'s body: the run, and the error it failed with (None: completed)."""
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
        benchmark = data.index_closes.get(INDEX_NAME, ())
        complete_run(session, run, result, root=root, benchmark=benchmark, variant=variant)
    except BacktestError as exc:
        fail_run(session, run, exc.code, variant)
        return run, exc
    except Exception as exc:
        # Whatever went wrong, the run stays a trial of its family (HAT-BT-030).
        fail_run(session, run, "backtest_error", variant)
        error = BacktestError("backtest_error")
        error.__cause__ = exc
        return run, error
    return run, None


@dataclass(frozen=True)
class RunRequest:
    """One backtest (`backtest_bot`): of a sleeve (`bot_id`: its strategy version, the live one
    unless `version_id` names another of the same strategy, on its universe), or, in research
    mode, of a strategy version (`version_id`) on a universe (`universe`, its id or its name
    among the strategy owner's universes) with no bot, so no broker account; the period, the
    fills (slippage and cost model), the cooldown replacing the policy's for a sweep, whether
    the run may evidence a version, and its conventions."""

    start: date
    end: date
    bot_id: uuid.UUID | None = None
    universe: str | None = None
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS
    cost_model: str = DEFAULT_COST_MODEL
    version_id: uuid.UUID | None = None
    cooldown_sessions: int | None = None
    for_promotion: bool = False
    conventions: RunConventions = field(default_factory=RunConventions)


def _sleeve_of(
    session: Session, request: RunRequest
) -> tuple[HatStrategyVersion, HatUniverseVersion, uuid.UUID]:
    """The run's strategy version, universe version and owner (whose risk policy sizes it):
    the bot's, or in research mode the version's and the named universe's; the universe version
    is the universe's live one (a sleeve reads the universe its pinned version belongs to). A
    request naming both a bot and a universe, or neither, is refused `backtest_bad_request`."""
    if (request.bot_id is None) == (request.universe is None):
        raise BacktestError("backtest_bad_request")
    if request.bot_id is not None:
        bot = session.get(HatBot, request.bot_id)
        if bot is None:
            raise BacktestError("backtest_unknown_bot")
        version = session.get(HatStrategyVersion, request.version_id or bot.live_version_id)
        if version is None or version.strategy_id != bot.strategy_id:
            raise BacktestError("backtest_unknown_version")
        live = _live_version(session, universe_of(session, bot))
        if live is None:
            raise BacktestError("backtest_no_universe")
        return version, live, bot.user_id
    version = (
        None if request.version_id is None else session.get(HatStrategyVersion, request.version_id)
    )
    strategy = None if version is None else session.get(HatStrategy, version.strategy_id)
    if version is None or strategy is None:
        raise BacktestError("backtest_unknown_version")
    universe = _universe_named(session, request.universe or "", strategy.user_id)
    live = _live_version(session, None if universe is None else universe.id)
    if live is None:
        raise BacktestError("backtest_no_universe")
    return version, live, strategy.user_id


def _live_version(session: Session, universe_id: uuid.UUID | None) -> HatUniverseVersion | None:
    """The universe's live version (None without a universe or a live version)."""
    if universe_id is None:
        return None
    return session.scalars(
        select(HatUniverseVersion).where(
            HatUniverseVersion.universe_id == universe_id, HatUniverseVersion.status == "live"
        )
    ).first()


def _universe_named(session: Session, text: str, user_id: uuid.UUID) -> HatUniverse | None:
    """The universe a research run names among the user's universes, by id or by name (None
    when none of them, or more than one, has it): another owner's universe is never read."""
    try:
        named = HatUniverse.id == uuid.UUID(text)
    except ValueError:
        named = HatUniverse.name == text
    found = session.scalars(select(HatUniverse).where(HatUniverse.user_id == user_id, named)).all()
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class _Target:
    """What every run of one request shares: the strategy version and its parsed config, the
    policy version, the universe, the fills and the stored regime table."""

    version_id: uuid.UUID
    config: StrategyConfig
    policy: PolicyVersion
    universe_id: uuid.UUID
    universe_version_id: uuid.UUID
    fills: BacktestFills
    regime_table: Mapping[str, Decimal]


def _target(session: Session, request: RunRequest) -> _Target:
    """The request's version, universe (`_sleeve_of`), owner's policy (under its cooldown) and
    fills; refusals raise `BacktestError` and record nothing."""
    version, universe, user_id = _sleeve_of(session, request)
    policy = effective_version(load_versions(session, user_id), utc_now())
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
    table = stored_regime_table(session, market_id_of(session), regime_profile(config.bot))
    return _Target(version.id, config, policy, universe.universe_id, universe.id, fills, table)


def _spec(target: _Target, request: RunRequest, conventions: RunConventions) -> RunSpec:
    """The run of `target` under `conventions`, its overrides applied to the config and the
    policy; refuses a promotion run's conventions, a missing base weight and a refused
    override (`BacktestError`)."""
    overrides = conventions.overrides
    if overrides.touches_identity():
        raise BacktestError("backtest_override_identity")
    try:
        config = overrides.apply_config(target.config)
        policy = overrides.apply_policy(target.policy)
    except OverrideError as exc:
        raise BacktestError(exc.code) from None
    return RunSpec(
        config=config,
        strategy_version_id=target.version_id,
        policy=policy,
        start=request.start,
        end=request.end,
        fills=target.fills,
        for_promotion=request.for_promotion,
        conventions=conventions,
        regime_table=target.regime_table,
    )


def _load(
    session: Session,
    store: DataStore,
    request: RunRequest,
    target: _Target,
    specs: Sequence[RunSpec],
) -> tuple[MarketData, dict[str, Any]]:
    """The one market every one of `specs` runs on: their regime index (one for all, else
    `backtest_sweep_mixed_index`), with the regime engine's inputs when any sizes under
    `policy`."""
    indices = {spec.config.regime.index for spec in specs}
    if len(indices) != 1:
        raise BacktestError("backtest_sweep_mixed_index")
    return load_market(
        store,
        session,
        universe_version_id=target.universe_version_id,
        index=indices.pop(),
        start=request.start,
        end=request.end,
        regime_inputs=any(s.conventions.regime_mult == REGIME_MULT_POLICY for s in specs),
    )


def backtest_bot(
    session: Session, store: DataStore, request: RunRequest, *, root: Path
) -> HatBacktestRun:
    """Backtest a sleeve's strategy version on the sleeve's universe, or in research mode a
    strategy version on a universe (`RunRequest`), under the owner's risk policy in force now,
    over the Parquet store, with the request's overrides (`overrides`); the `policy` regime rule
    also reads the regime engine's index, VIX and breadth, and sizes with the stored regime
    table live allocates with. The command line's body. Refusals before the run is recorded (an
    unknown bot, version or universe, conventions a promotion run may not use, a refused
    override, ...) raise `BacktestError` and record nothing; a refusal of the engine is a failed
    trial."""
    target = _target(session, request)
    spec = _spec(target, request, request.conventions)
    data, snapshot = _load(session, store, request, target, [spec])
    return record_backtest(
        session, spec, data, universe_id=target.universe_id, data_snapshot=snapshot, root=root
    )


@dataclass(frozen=True)
class Variant:
    """One run of a sweep: a label and its conventions (the overrides among them)."""

    label: str
    conventions: RunConventions


VARIANT_KEYS = frozenset(
    {"label", "set", "risk_pct", "max_positions", "sleeve_weight", "slot_order"}
)


def parse_variants(doc: object, base: RunConventions) -> list[Variant]:
    """A sweep's variants from its file's document: a list of mappings, each a unique `label`
    and any of `set` (a list of `path=value` strings, `overrides.parse_set`, after the base's),
    `risk_pct`, `max_positions`, `sleeve_weight` and `slot_order` (each replacing the base's).
    `backtest_sweep_bad_variants` for any other shape; a bad value raises the code its
    conventions or override raise."""
    if not isinstance(doc, list) or not doc:
        raise BacktestError("backtest_sweep_bad_variants")
    out = []
    for item in doc:
        if not isinstance(item, dict) or not set(item) <= VARIANT_KEYS:
            raise BacktestError("backtest_sweep_bad_variants")
        label, sets = item.get("label"), item.get("set", [])
        if not isinstance(label, str) or not label or not isinstance(sets, list):
            raise BacktestError("backtest_sweep_bad_variants")
        try:
            parsed = tuple(parse_set(text) for text in sets if isinstance(text, str))
        except OverrideError as exc:
            raise BacktestError(exc.code) from None
        if len(parsed) != len(sets):
            raise BacktestError("backtest_sweep_bad_variants")
        overrides = Overrides(
            sets=(*base.overrides.sets, *parsed),
            risk_pct=_number(item, "risk_pct", base.overrides.risk_pct),
            max_positions=_count(item, "max_positions", base.overrides.max_positions),
        )
        conventions = replace(
            base,
            overrides=overrides,
            sleeve_weight=_number(item, "sleeve_weight", base.sleeve_weight),
            slot_order=str(item.get("slot_order", base.slot_order)),
        )
        out.append(Variant(label, conventions))
    if len({v.label for v in out}) != len(out):
        raise BacktestError("backtest_sweep_bad_variants")
    return out


def _number(item: Mapping[str, object], key: str, default: Decimal | None) -> Decimal | None:
    if key not in item:
        return default
    found = exact(item[key], floats=True)
    if found is None:
        raise BacktestError("backtest_sweep_bad_variants")
    return found


def _count(item: Mapping[str, object], key: str, default: int | None) -> int | None:
    if key not in item:
        return default
    found = item[key]
    if isinstance(found, bool) or not isinstance(found, int):
        raise BacktestError("backtest_sweep_bad_variants")
    return found


@dataclass(frozen=True)
class SweepOutcome:
    """A sweep variant's run (None when it was refused before it was recorded) and the refusal
    or failure code (None for a completed run)."""

    label: str
    run: HatBacktestRun | None
    code: str | None


def sweep_bot(
    session: Session,
    store: DataStore,
    request: RunRequest,
    variants: Sequence[Variant],
    *,
    root: Path,
    after: Callable[[SweepOutcome], None] = lambda outcome: None,
) -> list[SweepOutcome]:
    """Run each variant of `request` (its conventions in place of the request's) over one load of
    the market, in order, each recorded as a trial of the family with its label and the sweep's
    id in `metrics.variant` (so a later run sees the earlier ones in its DSR trial count and PBO
    variants); `after` takes each variant's outcome as it is decided, in order, refused ones
    too (the command line commits and prints it there, so a later failure keeps the earlier
    trials and a reader sees the sweep's progress). A variant refused before it is recorded (a
    bad override, a missing weight) is skipped with its code; the request-wide refusals, and a
    sweep whose variants read different regime indices, raise `BacktestError` before any run."""
    if not variants or len({v.label for v in variants}) != len(variants):
        raise BacktestError("backtest_sweep_bad_variants")
    target = _target(session, request)
    specs: list[tuple[Variant, RunSpec | str]] = []
    for variant in variants:
        try:
            specs.append((variant, _spec(target, request, variant.conventions)))
        except BacktestError as exc:
            specs.append((variant, exc.code))
    runnable = [spec for _, spec in specs if isinstance(spec, RunSpec)]
    if not runnable:
        refused = [SweepOutcome(v.label, None, str(code)) for v, code in specs]
        for outcome in refused:
            after(outcome)
        return refused
    data, snapshot = _load(session, store, request, target, runnable)
    sweep_id = str(uuid.uuid4())
    out: list[SweepOutcome] = []
    for variant, spec in specs:
        if not isinstance(spec, RunSpec):
            out.append(SweepOutcome(variant.label, None, spec))
            after(out[-1])
            continue
        run, error = _record(
            session,
            spec,
            data,
            universe_id=target.universe_id,
            data_snapshot=snapshot,
            root=root,
            variant={"label": variant.label, "sweep_id": sweep_id},
        )
        out.append(SweepOutcome(variant.label, run, None if error is None else error.code))
        after(out[-1])
    return out
