"""Backtest runs: one run or a sweep of variants over one load of the market, each published as
its run folder (`folder`, docs/backtests/result-standard.md) under `HAT_DATA_ROOT/backtests/
runs/<run_id>/`, and `--rerun` of a run from its folder's `config.json`.

A backtest reads the strategy version, the risk policy, the universe and the reference data
from the database and writes nothing to it (owner, 2026-10-26): its only output is its folder,
and the trial registry (`trials`) beside the folders. A run's id is a UUIDv7, so the ids sort by
start time. A run that completes publishes `run.json`, `config.json` and the five CSV files; a
run that fails (a refusal of the engine, an error) or is stopped publishes `run.json` with its
status and `config.json`.

A run's deflated Sharpe probability is scored here, at its completion, through the family
floors (`dsr.family_deflated_sharpe`), over its trial family in the registry (`trials`: the
distinct `resolved_hash` values among the completed in-sample runs of its strategy, universe and
window, this one included, and the prior trials seeded there; a hold-out run reads the family
of the run it validates), with the variance of their Sharpe ratios; `dsr_n_trials` records the
count and `dsr_n_trials_effective` the count the floors scored it at.
"""

from __future__ import annotations

import csv
import io
import logging
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.paths import PROJECT_ROOT

from ..confighash import config_hash
from ..configs.records import Bot, Strategy, StrategyVersion
from ..configs.unit import configs_of
from ..costs import (
    DEFAULT_COST_MODEL,
    DEFAULT_SLIPPAGE_BPS,
    BacktestFills,
    CostError,
    check_slippage,
    load_cost_schedule,
)
from ..data import audit as data_audit
from ..data import golden, verify
from ..data.index_history import NIFTY_500_TRI
from ..data.store import DataStore
from ..engine.portfolio.common import market_id_of
from ..engine.portfolio.regime_service import regime_profile, stored_regime_table
from ..engine.strategy.config import ConfigError, StrategyConfig, parse_config
from ..models import HatUniverse, HatUniverseVersion
from ..money import exact
from ..risk.policy import PolicyError, PolicyVersion, RiskPolicy, effective_version, parse_policy
from ..risk.policy_upkeep import load_versions
from ..time import utc_now
from . import etf_data, folder, trials
from .data import MarketData, load_market
from .discipline import HOLD_OUT, changed_knobs, check_hold_out, knobs_of, resolve_knobs
from .dsr import MIN_TRIALS, PBO_BLOCKS, family_deflated_sharpe, pbo, trial_variance
from .ep_log import uses_results
from .evidence import (
    DSR_DIGITS,
    RunMetrics,
    evidence_doc,
    universe_live_version,
    universe_of,
)
from .loop import run_backtest
from .overlay import EtfData, OverlayConfig, OverlayError, replay_overlay
from .overrides import OverrideError, Overrides, parse_policy_set, parse_set, read_policy_file
from .run_audit import audit_run
from .spec import (
    ENTRY_BARS_5M,
    INITIAL_EQUITY,
    REGIME_MULT_POLICY,
    VOLUME_PACE_LIVE,
    BacktestError,
    CoSleeve,
    RunConventions,
    RunSpec,
    known_gaps,
    with_cooldown,
)
from .standard import FORMAT_VERSION, changed_paths, daily_returns, distinct_on_common

__all__ = [
    "HOLD_OUT",
    "IN_SAMPLE",
    "STRATEGY_LABELS",
    "CoSleeveRequest",
    "Labels",
    "RunOutcome",
    "RunRequest",
    "Sweep",
    "TrialBudget",
    "Variant",
    "backtest_bot",
    "check_one_change",
    "check_paths",
    "engine_commit",
    "hold_out",
    "parse_variants",
    "rerun",
    "resolved_of",
    "sweep_bot",
]

logger = logging.getLogger(__name__)

IN_SAMPLE = "in_sample"
SPLITS = (IN_SAMPLE, HOLD_OUT)
COMPLETE, FAILED, STOPPED = folder.COMPLETE, folder.FAILED, folder.STOPPED
# The strategies' short names in the result files (decisions.md, Two trading sleeves).
STRATEGY_LABELS: Mapping[str, str] = {
    "stage2-contraction-breakout": "S1",
    "momentum-breakout-ep": "S2",
    "darvas-box-breakout": "S3",
    "trend-pullback-ema": "S4",
    "rsi2-uptrend-reversion": "S5",
}
BENCHMARK_LABEL = "NIFTY500_TRI"


@dataclass(frozen=True)
class Labels:
    """What a run is called and compared with (run.json `identity` and `data`): its sweep and
    variant, its base (a run id; a sweep resolves a label of its file to the run it made), the
    one-line change against it, the reason of a declared bundle (a variant changing more than
    one knob, `discipline`), its split and, for a hold-out run, the run it validates."""

    sweep_id: str | None = None
    sweep_name: str | None = None
    variant: str | None = None
    base: str | None = None
    change: str | None = None
    split: str = IN_SAMPLE
    validates: str | None = None
    bundle: str | None = None

    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise BacktestError("backtest_bad_split")
        if (self.split == HOLD_OUT) != (self.validates is not None):
            raise BacktestError("backtest_hold_out_validates")


@dataclass(frozen=True)
class CoSleeveRequest:
    """A strategy run beside the request's own on its account (a portfolio run): its version
    (one of the same owner's strategies), its weight of the account and its config overrides
    (the policy is the account's: a co-sleeve sets none of it)."""

    version_id: uuid.UUID
    weight: Decimal
    overrides: Overrides = field(default_factory=Overrides)


@dataclass(frozen=True)
class RunRequest:
    """One backtest (`backtest_bot`): of a sleeve (`bot_id`: its strategy version, the live one
    unless `version_id` names another of the same strategy, on its universe), or, in research
    mode, of a strategy version (`version_id`) on a universe (`universe`, its id or its name
    among the strategy owner's universes) with no bot, so no broker account; the period, the
    fills (slippage and cost model), the cooldown replacing the policy's for a sweep, whether
    the run may evidence a version, its conventions and labels, the command line's arguments
    (recorded in `config.json`) and, for a portfolio run (`portfolio`), the strategies beside
    it on the same account and the ETF overlay."""

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
    labels: Labels = field(default_factory=Labels)
    args: tuple[str, ...] = ()
    co_sleeves: tuple[CoSleeveRequest, ...] = ()
    overlay: OverlayConfig | None = None


def _sleeve_of(
    session: Session, request: RunRequest
) -> tuple[StrategyVersion, HatUniverseVersion, uuid.UUID]:
    """The run's strategy version, universe version and owner (whose risk policy sizes it):
    the bot's, or in research mode the version's and the named universe's; the universe version
    is the universe's live one (a sleeve reads the universe its pinned version belongs to). A
    request naming both a bot and a universe, or neither, is refused `backtest_bad_request`."""
    if (request.bot_id is None) == (request.universe is None):
        raise BacktestError("backtest_bad_request")
    configs = configs_of(session)
    if request.bot_id is not None:
        bot = configs.get(Bot, request.bot_id)
        if bot is None:
            raise BacktestError("backtest_unknown_bot")
        version = configs.get(StrategyVersion, request.version_id or bot.live_version_id)
        if version is None or version.strategy_id != bot.strategy_id:
            raise BacktestError("backtest_unknown_version")
        live = universe_live_version(session, universe_of(session, bot))
        if live is None:
            raise BacktestError("backtest_no_universe")
        return version, live, bot.user_id
    version = configs.get(StrategyVersion, request.version_id)
    strategy = None if version is None else configs.get(Strategy, version.strategy_id)
    if version is None or strategy is None:
        raise BacktestError("backtest_unknown_version")
    universe = _universe_named(session, request.universe or "", strategy.user_id)
    live = universe_live_version(session, None if universe is None else universe.id)
    if live is None:
        raise BacktestError("backtest_no_universe")
    return version, live, strategy.user_id


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
    policy version, the universe, the fills, the stored regime table and the names the result
    files carry."""

    version_id: uuid.UUID
    strategy_id: uuid.UUID
    config: StrategyConfig
    policy: PolicyVersion
    universe_id: uuid.UUID
    universe_version_id: uuid.UUID
    fills: BacktestFills
    regime_table: Mapping[str, Decimal]
    strategy: str
    universe: str
    co: tuple[_CoTarget, ...] = ()


@dataclass(frozen=True)
class _CoTarget:
    """A co-sleeve's strategy version, its parsed config, its stored regime table and its
    strategy's short name."""

    version_id: uuid.UUID
    strategy_id: uuid.UUID
    config: StrategyConfig
    regime_table: Mapping[str, Decimal]
    strategy: str


def _co_target(session: Session, version_id: uuid.UUID, user_id: uuid.UUID) -> _CoTarget:
    """A co-sleeve's version, one of `user_id`'s strategies (`backtest_unknown_version`
    else)."""
    configs = configs_of(session)
    version = configs.get(StrategyVersion, version_id)
    strategy = None if version is None else configs.get(Strategy, version.strategy_id)
    if version is None or strategy is None or strategy.user_id != user_id:
        raise BacktestError("backtest_unknown_version")
    config = parse_config(version.config)
    table = stored_regime_table(session, market_id_of(session), regime_profile(config.bot))
    return _CoTarget(version.id, version.strategy_id, config, table, _strategy_label(strategy.name))


def _strategy_label(name: str) -> str:
    return STRATEGY_LABELS.get(name, name)


def _universe_label(version: HatUniverseVersion, name: str) -> str:
    """`<index without spaces>_PIT` for a universe read from its index point in time (e.g.
    `NIFTY500_PIT`), else the universe's name."""
    index = (version.definition or {}).get("index")
    return f"{str(index).replace(' ', '').upper()}_PIT" if index else name


def _target(session: Session, request: RunRequest) -> _Target:
    """The request's version, universe (`_sleeve_of`), owner's policy (under its cooldown) and
    fills; refusals raise `BacktestError`."""
    version, universe, user_id = _sleeve_of(session, request)
    policy = effective_version(load_versions(session, user_id), utc_now())
    if policy is None:
        raise BacktestError("backtest_no_policy")
    if request.cooldown_sessions is not None:
        policy = with_cooldown(policy, request.cooldown_sessions)
    config = parse_config(version.config)
    fills = _fills(request.cost_model, request.slippage_bps, request.start)
    table = stored_regime_table(session, market_id_of(session), regime_profile(config.bot))
    strategy = configs_of(session).get(Strategy, version.strategy_id)
    named = session.get(HatUniverse, universe.universe_id)
    co = tuple(_co_target(session, c.version_id, user_id) for c in request.co_sleeves)
    label = _strategy_label("" if strategy is None else strategy.name)
    return _Target(
        version.id,
        version.strategy_id,
        config,
        policy,
        universe.universe_id,
        universe.id,
        fills,
        table,
        # a portfolio is its own trial family: its strategies' names joined
        "+".join([label, *(c.strategy for c in co)]),
        _universe_label(universe, "" if named is None else named.name),
        co,
    )


def _fills(cost_model: str, slippage_bps: Decimal, start: date) -> BacktestFills:
    try:
        fills = BacktestFills(load_cost_schedule(cost_model), check_slippage(slippage_bps))
        fills.schedule.on(start)  # a run's first day has charges in force
    except CostError as exc:
        raise BacktestError(exc.code) from None
    return fills


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
    if request.cooldown_sessions is not None:  # a candidate policy replaced the one it was set on
        policy = with_cooldown(policy, request.cooldown_sessions)
    co_sleeves = tuple(_co_sleeve(t, c) for t, c in zip(target.co, request.co_sleeves, strict=True))
    return RunSpec(
        config=config,
        strategy_version_id=target.version_id,
        policy=policy,
        start=request.start,
        end=request.end,
        fills=target.fills,
        initial_equity=conventions.start_capital,
        for_promotion=request.for_promotion,
        conventions=conventions,
        regime_table=target.regime_table,
        co_sleeves=co_sleeves,
        overlay=request.overlay,
    )


def _co_sleeve(target: _CoTarget, request: CoSleeveRequest) -> CoSleeve:
    """A co-sleeve's run: its config under its overrides; one that names the config's identity
    or sets the policy is refused (`backtest_override_identity`, `backtest_co_sleeve_policy`)."""
    overrides = request.overrides
    if overrides.touches_identity():
        raise BacktestError("backtest_override_identity")
    if overrides.touches_policy:
        raise BacktestError("backtest_co_sleeve_policy")
    try:
        config = overrides.apply_config(target.config)
    except OverrideError as exc:
        raise BacktestError(exc.code) from None
    return CoSleeve(config, target.version_id, request.weight, target.regime_table, overrides)


def _configs(spec: RunSpec) -> list[StrategyConfig]:
    """Every strategy config a run trades: its own and its co-sleeves'."""
    return [spec.config, *(c.config for c in spec.co_sleeves)]


@dataclass(frozen=True)
class _Market:
    """The market a request's runs read, its data snapshot, and its data audit
    (`data.audit`)."""

    data: MarketData
    snapshot: dict[str, Any]
    audit: dict[str, Any]
    etf: EtfData | None = None  # an overlay run's (`etf_data.load`)


def _load(
    session: Session,
    store: DataStore,
    universe_version_id: uuid.UUID,
    period: tuple[date, date],
    specs: Sequence[RunSpec],
    root: Path,
) -> _Market:
    """The one market every one of `specs` runs on: their regime index (one for all, else
    `backtest_sweep_mixed_index`), with the regime engine's inputs when any sizes under
    `policy`; and its data audit, read from `data_audits/` when this data and window were
    audited before. `backtest_data_audit_failed` when the audit fails: no run starts on it."""
    indices = {spec.config.regime.index for spec in specs}
    if len(indices) != 1:
        raise BacktestError("backtest_sweep_mixed_index")
    data, snapshot = load_market(
        store,
        session,
        universe_version_id=universe_version_id,
        index=indices.pop(),
        start=period[0],
        end=period[1],
        regime_inputs=any(s.conventions.regime_mult == REGIME_MULT_POLICY for s in specs),
        # the EP log and `earnings_catalyst` read the stored results filings
        results=any(p.preopen for s in specs for c in _configs(s) for p in c.playbooks),
    )
    if not data.results and any(uses_results(c) for s in specs for c in _configs(s)):
        raise BacktestError("backtest_results_missing")
    etf = None
    if any(s.overlay is not None for s in specs):
        try:
            etf = etf_data.load(store.root, *period)
        except etf_data.EtfDataError as exc:
            raise BacktestError(exc.code) from None
    used_5m = data.intraday is not None and any(
        s.conventions.entry_bars == ENTRY_BARS_5M for s in specs
    )
    verified = verify.status_for(root, *period, used_5m=used_5m)
    key = data_audit.cache_key(
        str(snapshot["content_digest"]),
        *period,
        used_5m=used_5m,
        golden_digest=golden.current(root),
        verify_digest=verified.get("digest"),
    )
    audit, _ = data_audit.cached(
        root,
        key,
        lambda: data_audit.gather(
            store,
            session,
            data,
            snapshot,
            *period,
            used_5m=used_5m,
            verified=verified,
        ),
    )
    if audit.get("status") == data_audit.FAIL:
        logger.error("data audit %s failed: backtests refused", key)
        raise BacktestError("backtest_data_audit_failed")
    if etf is not None:  # what the overlay read, in config.json's snapshot (not its digest)
        snapshot = {**snapshot, "etf_store": etf_data.manifest(store.root)}
    return _Market(data, snapshot, audit, etf)


def _etf_files(snapshot: Mapping[str, Any]) -> object:
    """The ETF store files' digests a run's data snapshot recorded (None without an overlay)."""
    return (snapshot.get("etf_store") or {}).get("files")


def _cap_refusal(spec: RunSpec, audit: Mapping[str, Any]) -> str | None:
    """The refusal of a run whose per-industry cap binds while the shared data audit found
    member-days with no industry (`data_audit.industry_cap_refused`); None otherwise."""
    policy = spec.policy.policy
    refused = data_audit.industry_cap_refused(
        audit, policy.max_positions_per_industry, policy.max_open_positions
    )
    return data_audit.INDUSTRY_CAP_REFUSED if refused else None


def _audit_ref(audit: Mapping[str, Any]) -> dict[str, Any]:
    """What run.json's `data.audit` and config.json record of the data audit."""
    return {"digest": audit.get("digest"), "status": audit.get("status")}


def fill_ranges(
    data: MarketData, trades_csv: str | None
) -> dict[tuple[str, date], tuple[Decimal, Decimal]]:
    """The (low, high) on the price basis of each (instrument, date) a run's `trades.csv` fills
    on (entries and exits), for the run audit."""
    keys = {
        (r["instrument_id"], date.fromisoformat(d))
        for r in csv.DictReader(io.StringIO(trades_csv or ""))
        for d in (r["entry_date"], r["exit_date"])
        if d
    }
    if not keys:
        return {}
    wanted = pl.DataFrame(
        sorted(keys), schema={"instrument_id": pl.String, "date": pl.Date}, orient="row"
    )
    found = data.bars.select(
        pl.col("instrument_id").cast(pl.String), "date", "low_e4", "high_e4"
    ).join(wanted, on=["instrument_id", "date"])
    return {
        (i, d): (Decimal(low).scaleb(-4), Decimal(high).scaleb(-4))
        for i, d, low, high in found.iter_rows()
        if low is not None and high is not None
    }


# --- one run -----------------------------------------------------------------------------------


def engine_commit(root: Path = PROJECT_ROOT) -> str | None:
    """The repository's HEAD, with `-dirty` when its tree has changes; None outside a git
    checkout (the deployed image)."""
    try:
        head = subprocess.run(  # noqa: S603 - fixed arguments
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603 - fixed arguments
            ["git", "-C", str(root), "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{head}-dirty" if dirty else head or None


def resolved_of(spec: RunSpec) -> dict[str, Any]:
    """The full parameter set a run ran with (run.json `config.resolved`): the strategy config
    and the risk policy after its overrides, and its conventions (fills, slot order, sizing)
    without the overrides that produced them, so `resolved_hash` is equal for two runs that ran
    alike, however they were asked for."""
    conventions = {k: v for k, v in spec.recorded_conventions().items() if k != "overrides"}
    out = {
        "strategy": spec.config.to_json(),
        "risk_policy": spec.policy.policy.to_json(),
        "conventions": conventions,
    }
    if spec.co_sleeves:  # a portfolio's other strategies, as they ran
        conventions["co_sleeves"] = [
            {k: v for k, v in c.record().items() if k != "overrides"} for c in spec.co_sleeves
        ]
        out["co_sleeve_strategies"] = [c.config.to_json() for c in spec.co_sleeves]
    return out


def _capital(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass(frozen=True)
class RunOutcome:
    """A run's id (None when it was refused before it ran), status, refusal or failure code,
    its `run.json` document and, in a sweep with a trial budget, its family's own trials."""

    label: str | None
    run_id: str | None
    status: str
    code: str | None
    run: Mapping[str, Any] | None = None
    trials_own: int | None = None  # a budgeted sweep's: the family's own trials after it


@dataclass(frozen=True)
class _Plan:
    """One run to execute: its spec, labels (the base resolved to a run id), what
    `config.json` records of how it was asked for, its overrides block and its knobs
    (`discipline.knobs_of`; a rerun's are the run's it replays)."""

    spec: RunSpec
    labels: Labels
    asked: Mapping[str, Any]
    overrides: Mapping[str, Any]
    knobs: Mapping[str, str]


def _config_doc(
    run_id: str, target: _Target, plan: _Plan, snapshot: Mapping[str, Any], audit: Mapping[str, Any]
) -> dict[str, Any]:
    """`config.json`: everything `--rerun` needs, from this file alone."""
    spec = plan.spec
    c = spec.conventions
    return {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "strategy": target.strategy,
        "strategy_id": str(target.strategy_id),
        "strategy_version_id": str(spec.strategy_version_id),
        "strategy_config": spec.config.to_json(),
        "risk_policy": {
            "version_id": str(spec.policy.version_id),
            "version": spec.policy.version,
            "document": spec.policy.policy.to_json(),
        },
        "universe": {
            "universe_id": str(target.universe_id),
            "universe_version_id": str(target.universe_version_id),
            "label": target.universe,
        },
        "period": {"start": spec.start.isoformat(), "end": spec.end.isoformat()},
        "fills": {
            "slippage_bps": str(spec.fills.slippage_bps),
            "cost_model": spec.fills.schedule.version,
        },
        "for_promotion": spec.for_promotion,
        "known_gaps": known_gaps(spec),
        "conventions": {
            "entry_fill_rule": c.entry_fill_rule,
            "slot_order": c.slot_order,
            "ambiguous": c.ambiguous,
            "suspension_sessions": c.suspension_sessions,
            "sleeve_weight": None if c.sleeve_weight is None else str(c.sleeve_weight),
            "regime_mult": c.regime_mult,
            "regime_table": {k: str(v) for k, v in spec.regime_table.items()},
            "start_capital": str(spec.initial_equity),
            "entry_bars": c.entry_bars,
            "volume_pace": c.volume_pace,
        },
        "labels": {
            "sweep_id": plan.labels.sweep_id,
            "sweep_name": plan.labels.sweep_name,
            "variant": plan.labels.variant,
            "base_run_id": plan.labels.base,
            "change": plan.labels.change,
            "split": plan.labels.split,
            "validates_run_id": plan.labels.validates,
            "bundle": plan.labels.bundle,
        },
        "knobs": dict(plan.knobs),
        "asked": dict(plan.asked),
        "data_snapshot_id": snapshot.get("content_digest"),
        "data_snapshot": dict(snapshot),  # what was read: the store, membership and its cover
        "data_audit": _audit_ref(audit),
        **_portfolio_doc(target, spec),
    }


def _portfolio_doc(target: _Target, spec: RunSpec) -> dict[str, Any]:
    """`config.json`'s co-sleeves (each as `_replayed` reads it back) and overlay, only when
    the run has them."""
    out: dict[str, Any] = {}
    if spec.co_sleeves:
        out["co_sleeves"] = [
            {
                "strategy": t.strategy,
                "strategy_id": str(t.strategy_id),
                "strategy_version_id": str(c.strategy_version_id),
                "strategy_config": c.config.to_json(),
                "weight": str(c.weight),
                "regime_table": {k: str(v) for k, v in c.regime_table.items()},
                "overrides": c.overrides.record(),
            }
            for t, c in zip(target.co, spec.co_sleeves, strict=True)
        ]
    if spec.overlay is not None:
        out["overlay"] = spec.overlay.record()
    return out


def _blocks(
    run_id: str,
    target: _Target,
    plan: _Plan,
    market: _Market,
    root: Path,
    status: str,
    error: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """run.json's `identity`, `data` and `config` blocks."""
    spec, labels = plan.spec, plan.labels
    resolved = resolved_of(spec)
    base = None if labels.base is None else folder.read_run(root, labels.base)
    base_resolved = None if base is None else (base.get("config") or {}).get("resolved")
    base_config = None if labels.base is None else folder.read_config(root, labels.base)
    base_knobs = None if base_config is None else base_config.get("knobs")
    base_policy = None if base_config is None else (base_config.get("risk_policy") or {})
    identity = {
        "run_id": run_id,
        "strategy": target.strategy,
        "strategy_version": str(spec.strategy_version_id),
        "engine_commit": engine_commit(),
        "sweep_id": labels.sweep_id,
        "sweep_name": labels.sweep_name,
        "variant": labels.variant,
        "base_run_id": labels.base,
        "change": labels.change,
        # None without a base, or when the base's config.json predates knobs
        "changed_knobs": changed_knobs(
            plan.knobs, resolve_knobs(base_knobs, target.config.to_json())
        )
        if isinstance(base_knobs, dict)
        else None,
        "bundle": labels.bundle,
        "created_at": utc_now().isoformat(),
        "status": status,
        "error": error,
        "for_promotion": spec.for_promotion,
    }
    data = {
        "universe": target.universe,
        "data_snapshot_id": market.snapshot.get("content_digest"),
        "split": labels.split,
        "validates_run_id": labels.validates,
        "period_start": spec.start.isoformat(),
        "period_end": spec.end.isoformat(),
        "benchmark": BENCHMARK_LABEL,
        "audit": _audit_ref(market.audit),
        **data_audit.run_counts(market.audit),
    }
    config = {
        "overrides": changed_paths(resolved, base_resolved)
        if isinstance(base_resolved, dict)
        else dict(plan.overrides),
        "resolved": resolved,
        "resolved_hash": config_hash(resolved),
        "risk_policy_version": spec.policy.version,
        "base_risk_policy_version": None if base_policy is None else base_policy.get("version"),
        "cost_model": spec.fills.schedule.version,
        "start_capital": _capital(spec.initial_equity),
        "rf_pct": 0,
    }
    return identity, data, config


# [A] fewer distinct variants than this make CSCV's rank a coin toss (with 2, PBO is 0 or 1 by
# which half wins): the family's PBO is null below it.
MIN_PBO_VARIANTS = 4


def family_pbo(
    root: Path,
    strategy: str,
    universe: str,
    window: tuple[date, date],
    own: Mapping[date, float],
    blocks: int = PBO_BLOCKS,
) -> float | None:
    """The probability of backtest overfitting (`dsr.pbo`, CSCV) of a run's family: the
    completed in-sample runs of its strategy on its universe and window with a readable equity
    file, and the run itself (`own`, its daily returns), on the sessions all of them have a
    return for, a rerun of the same variant counted once. None with fewer than
    `MIN_PBO_VARIANTS` distinct variants, fewer than 2 common sessions per block, or a series
    CSCV cannot score."""
    period = (window[0].isoformat(), window[1].isoformat())
    series = [
        found
        for doc in folder.list_runs(root)
        if (doc.get("identity") or {}).get("strategy") == strategy
        and (doc.get("identity") or {}).get("status") == COMPLETE
        and (doc.get("data") or {}).get("universe") == universe
        and (doc.get("data") or {}).get("split") == IN_SAMPLE
        and ((doc.get("data") or {}).get("period_start"), (doc.get("data") or {}).get("period_end"))
        == period
        and (found := folder.read_returns(root, str(doc["identity"].get("run_id")))) is not None
    ]
    distinct, sessions = distinct_on_common([*series, own])
    if len(distinct) < MIN_PBO_VARIANTS or sessions < 2 * blocks:
        return None
    found = pbo(distinct, blocks)
    return None if found is None else found.prob


def trial_family(root: Path, strategy: str, universe: str, spec: RunSpec, labels: Labels) -> str:
    """The registry key a run counts its trials in: its own strategy, universe and window, or for
    a hold-out run the window of the in-sample run it validates (when its folder is readable)."""
    start, end = spec.start.isoformat(), spec.end.isoformat()
    if labels.split != IN_SAMPLE and labels.validates is not None:
        data = (folder.read_run(root, labels.validates) or {}).get("data") or {}
        start, end = data.get("period_start") or start, data.get("period_end") or end
    return trials.family_key(strategy, universe, start, end)


def _execute(
    target: _Target,
    plan: _Plan,
    market: _Market,
    *,
    root: Path,
    label: str | None = None,
) -> RunOutcome:
    """Run one plan and publish its folder: complete, with its run audit (`audit.json`), or
    failed with the engine's refusal code (`backtest_error` for anything else, chained); a stop
    (Ctrl-C) publishes `stopped_early` and goes on stopping. Every folder holds the data audit
    it ran on (`data_audit.json`). A completed in-sample run joins the trial registry."""
    data = market.data
    run_id = str(uuid.uuid7())
    spec, labels = plan.spec, plan.labels
    config_doc = _config_doc(run_id, target, plan, market.snapshot, market.audit)
    docs: dict[str, Mapping[str, Any]] = {"data_audit": market.audit}

    def inputs(status: str, error: str | None, **extra: Any) -> folder.RunInputs:  # noqa: ANN401
        identity, data_block, config = _blocks(run_id, target, plan, market, root, status, error)
        return folder.RunInputs(
            identity=identity,
            data=data_block,
            config=config,
            start_capital=spec.initial_equity,
            policy_max_positions=spec.policy.policy.max_open_positions,
            **extra,
        )

    def give_up(status: str, code: str) -> RunOutcome:
        doc = folder.failed_run(inputs(status, code))
        folder.publish(root, run_id, doc, config_doc, docs=docs)
        return RunOutcome(label, run_id, status, code, doc)

    try:
        result = run_backtest(spec, data, market.etf)
        # what the run took for estimates, known only now (`spec.known_gaps`)
        config_doc = {**config_doc, "known_gaps": result.metrics["known_gaps"]}
        resolved_hash = config_hash(resolved_of(spec))
        stats = RunMetrics.from_json(result.metrics).returns
        own = (resolved_hash, None if stats is None else stats.sr)
        key = trial_family(root, target.strategy, target.universe, spec, labels)
        count, sharpes = trials.family(
            trials.read_registry(root), key, own if labels.split == IN_SAMPLE else None
        )
        deflated = family_deflated_sharpe(stats, count, trial_variance(sharpes))
        built = inputs(
            COMPLETE,
            None,
            benchmark=data.index_closes.get(NIFTY_500_TRI, ()),
            base_trades=None if labels.base is None else folder.trade_keys(root, labels.base),
            dsr_prob=deflated.prob,
            dsr_n_trials=count,
            dsr_n_trials_effective=max(count, MIN_TRIALS),
            pbo=family_pbo(
                root,
                target.strategy,
                target.universe,
                (spec.start, spec.end),
                daily_returns(result.equity, spec.initial_equity),
            ),
        )
        doc, csvs = folder.build(run_id, result, built)
        audit = audit_run(
            doc,
            csvs,
            fill_ranges(data, csvs.get("trades")),
            spec.fills.slippage_bps,
            data_audit=market.audit,
            explained=data_audit.explanation_of(root, market.audit.get("digest")),
        )
        pbo = built.pbo
        evidence = evidence_doc(
            run_id=run_id,
            strategy=target.strategy,
            resolved_hash=resolved_hash,
            strategy_id=target.strategy_id,
            strategy_version_id=spec.strategy_version_id,
            universe_id=target.universe_id,
            membership=(market.snapshot.get("membership") or {}).get("universe_key"),
            metrics={
                **result.metrics,
                "dsr": {"prob": deflated.prob, "trials": count, "reason": deflated.reason},
                "pbo": None if pbo is None else {"prob": str(round(pbo, DSR_DIGITS))},
                "completed_at": utc_now().isoformat(),
            },
            trades=((t.playbook, t.score, t.r_multiple) for t in result.trades),
        )
        docs = {**docs, "audit": audit, "evidence": evidence}
        folder.publish(root, run_id, doc, config_doc, csvs, docs=docs)
    except BacktestError as exc:
        return give_up(FAILED, exc.code)
    except KeyboardInterrupt:
        give_up(STOPPED, "backtest_stopped")
        raise
    except Exception:
        # whatever went wrong, the run's folder says it failed; the traceback goes to the log
        logger.exception("backtest run %s failed", run_id)
        return give_up(FAILED, "backtest_error")
    if labels.split == IN_SAMPLE:
        trials.record_trial(root, key, resolved_hash, run_id, own[1])
    return RunOutcome(label, run_id, COMPLETE, None, doc)


def _knobs(request: RunRequest, conventions: RunConventions, target: _Target) -> dict[str, str]:
    """The run's knobs (`discipline.knobs_of`), its `set` paths resolved in the version's
    config (`discipline.resolve_knobs`)."""
    knobs = knobs_of(
        conventions, request.slippage_bps, request.cost_model, request.cooldown_sessions
    )
    return resolve_knobs(knobs, target.config.to_json())


def _plan(
    request: RunRequest, spec: RunSpec, labels: Labels, asked: Mapping[str, Any], target: _Target
) -> _Plan:
    overrides = spec.conventions.overrides.record()
    return _Plan(spec, labels, asked, overrides, _knobs(request, spec.conventions, target))


def _guard_hold_out(root: Path, spec: RunSpec, labels: Labels) -> None:
    """A hold-out run's guards (`discipline.check_hold_out`); nothing for an in-sample run."""
    if labels.split == HOLD_OUT and labels.validates is not None:
        check_hold_out(root, labels.validates, resolved_of(spec), spec.start)


def backtest_bot(
    session: Session, store: DataStore, request: RunRequest, *, root: Path
) -> RunOutcome:
    """Backtest a sleeve's strategy version on the sleeve's universe, or in research mode a
    strategy version on a universe (`RunRequest`), under the owner's risk policy in force now,
    over the Parquet store, with the request's overrides (`overrides`); the `policy` regime rule
    also reads the regime engine's index, VIX and breadth, and sizes with the stored regime
    table live allocates with. The command line's body. Refusals before the run starts (an
    unknown bot, version or universe, conventions a promotion run may not use, a refused
    override, ...) raise `BacktestError` and publish nothing; a refusal of the engine publishes
    a failed run."""
    target = _target(session, request)
    spec = _spec(target, request, request.conventions)
    _guard_hold_out(root, spec, request.labels)
    market = _load(
        session, store, target.universe_version_id, (request.start, request.end), [spec], root
    )
    refused = _cap_refusal(spec, market.audit)
    if refused is not None:
        raise BacktestError(refused)
    asked = {"args": list(request.args), "variant_item": None}
    return _execute(target, _plan(request, spec, request.labels, asked, target), market, root=root)


# --- sweeps ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One run of a sweep: a label, its conventions (the overrides among them), its base (a
    label earlier in the file or a run id; None: a baseline), the one-line change, the run a
    hold-out variant validates, its item as the file wrote it and, for a declared bundle, its
    reason."""

    label: str
    conventions: RunConventions
    base: str | None = None
    change: str | None = None
    validates: str | None = None
    item: Mapping[str, Any] = field(default_factory=dict)
    bundle: str | None = None


@dataclass(frozen=True)
class Sweep:
    """A variants file: the sweep's id and name and its variants in order."""

    sweep_id: str
    sweep_name: str
    variants: tuple[Variant, ...]


VARIANT_KEYS = frozenset(
    {
        "label",
        "base",
        "change",
        "validates",
        "bundle",
        "reason",
        "set",
        "playbooks",
        "risk_pct",
        "max_positions",
        "max_per_industry",
        "flat_ladder",
        "policy",
        "policy_set",
        "sleeve_weight",
        "slot_order",
        "entry_fill",
        "ambiguous",
        "suspension_sessions",
        "regime_mult",
        "start_capital",
        "entry_bars",
        "volume_pace",
    }
)
SWEEP_KEYS = frozenset({"sweep_id", "sweep_name", "variants"})


def _text(item: Mapping[str, object], key: str) -> str | None:
    found = item.get(key)
    if found is None:
        return None
    if not isinstance(found, str) or not found.strip():
        raise BacktestError("backtest_sweep_bad_variants")
    return found


def parse_variants(
    doc: object,
    base: RunConventions,
    read_policy: Callable[[str], RiskPolicy] = read_policy_file,
) -> Sweep:
    """A sweep from its variants file's document: a mapping of `sweep_id` (text), an optional
    `sweep_name` (the id when absent) and `variants`, a list of mappings, each a unique `label`
    and any of `base` (an earlier variant's label or a run id), `change` (one line),
    `validates` (a run id, for a hold-out sweep), `set` (a list of `path=value` strings,
    `overrides.parse_set`, after the base conventions'), `policy_set` (likewise,
    `overrides.parse_policy_set`), `policy` (a policy file's path, relative to the working
    directory, read by `read_policy`), `risk_pct`, `max_positions`, `max_per_industry`,
    `flat_ladder` (a boolean), `sleeve_weight`, `slot_order`, `entry_fill`, `ambiguous`,
    `suspension_sessions`, `regime_mult`, `start_capital`, `entry_bars` and `volume_pace` (each
    replacing the base's), and
    `bundle: true` with a one-line `reason` (a variant with a base only).
    `backtest_sweep_bad_variants` for any other shape; a bad value raises the code its
    conventions or override raise. Each override path, and one change per variant, are checked
    against the version's config before any variant runs (`check_paths`, `check_one_change`,
    `sweep_bot`)."""
    if not isinstance(doc, dict) or not set(doc) <= SWEEP_KEYS:
        raise BacktestError("backtest_sweep_bad_variants")
    sweep_id = _text(doc, "sweep_id")
    items = doc.get("variants")
    if sweep_id is None or not isinstance(items, list) or not items:
        raise BacktestError("backtest_sweep_bad_variants")
    out: list[Variant] = []
    for item in items:
        if not isinstance(item, dict) or not set(item) <= VARIANT_KEYS:
            raise BacktestError("backtest_sweep_bad_variants")
        label = _text(item, "label")
        based = _text(item, "base")
        change = _text(item, "change")
        if label is None or (change is not None and "\n" in change):
            raise BacktestError("backtest_sweep_bad_variants")
        earlier = {v.label: v for v in out}
        if based is not None and based not in earlier and not folder.is_run_id(based):
            raise BacktestError("backtest_sweep_bad_variants")  # a base the file has not run yet
        variant = Variant(
            label,
            _conventions(item, base, read_policy),
            based,
            change,
            _text(item, "validates"),
            dict(item),
            _bundle(item),
        )
        if variant.bundle is not None and based is None:
            raise BacktestError("backtest_sweep_bad_variants")
        out.append(variant)
    if len({v.label for v in out}) != len(out):
        raise BacktestError("backtest_sweep_bad_variants")
    return Sweep(sweep_id, _text(doc, "sweep_name") or sweep_id, tuple(out))


def _bundle(item: Mapping[str, object]) -> str | None:
    """A declared bundle's reason: `bundle: true` needs a one-line `reason`, and a `reason`
    needs the bundle."""
    reason = _text(item, "reason")
    declared = _flag(item, "bundle", False)
    if declared != (reason is not None) or (reason is not None and "\n" in reason):
        raise BacktestError("backtest_sweep_bad_variants")
    return reason


PATH_CODES = frozenset(
    {"backtest_bad_override_path", "backtest_override_no_item", "backtest_override_ambiguous"}
)


def check_paths(sweep: Sweep, config: StrategyConfig, policy: PolicyVersion) -> None:
    """Refuse a sweep (`BacktestError` with the override's code) with a variant whose `set` path
    names nothing in the version's `config` or whose `policy_set` path names no field of
    `policy` (or of its candidate policy), before any variant runs: a typo fails the whole
    sweep, not one variant of it."""
    for variant in sweep.variants:
        overrides = variant.conventions.overrides
        try:
            overrides.apply_config(config)
        except OverrideError as exc:
            _path_refusal(exc)
        try:
            overrides.apply_policy(policy)
        except OverrideError as exc:
            _path_refusal(exc)


def _path_refusal(exc: OverrideError) -> None:
    """Raise a path refusal as the sweep's; any other refusal is the variant's alone."""
    if exc.code in PATH_CODES:
        raise BacktestError(exc.code) from None


def check_one_change(sweep: Sweep, config: Mapping[str, Any]) -> None:
    """Refuse a sweep (`backtest_sweep_not_one_change`) with a variant whose knobs differ from
    those of its base earlier in the file in more than one, unless it is a declared bundle; the
    `set` paths resolved in the version's config `config` (`discipline.resolve_knobs`). The
    run's slippage, cost model and cooldown are the command line's, alike for both; a base
    named by run id is checked by `sweep_bot` against the knobs its `config.json` records."""
    labels = {v.label: v for v in sweep.variants}

    def knobs(variant: Variant) -> dict[str, str]:
        return resolve_knobs(knobs_of(variant.conventions, Decimal(0), "", None), config)

    for variant in sweep.variants:
        base = labels.get(variant.base or "")
        if (
            base is not None
            and variant.bundle is None
            and len(changed_knobs(knobs(variant), knobs(base))) > 1
        ):
            raise BacktestError("backtest_sweep_not_one_change")


def _conventions(
    item: Mapping[str, object], base: RunConventions, read_policy: Callable[[str], RiskPolicy]
) -> RunConventions:
    """A variant's conventions: the base's with the item's overrides on top."""
    parsed = _texts(item, "set", parse_set)
    policy_sets = _texts(item, "policy_set", parse_policy_set)
    policy = base.overrides.policy
    if "policy" in item:
        if not isinstance(item["policy"], str) or not item["policy"]:
            raise BacktestError("backtest_sweep_bad_variants")
        try:
            policy = read_policy(item["policy"])
        except OverrideError as exc:
            raise BacktestError(exc.code) from None
    overrides = Overrides(
        sets=(*base.overrides.sets, *parsed),
        playbooks=_names(item, "playbooks", base.overrides.playbooks),
        policy=policy,
        policy_sets=(*base.overrides.policy_sets, *policy_sets),
        risk_pct=_number(item, "risk_pct", base.overrides.risk_pct),
        max_positions=_count(item, "max_positions", base.overrides.max_positions),
        max_per_industry=_count(item, "max_per_industry", base.overrides.max_per_industry),
        flat_ladder=_flag(item, "flat_ladder", base.overrides.flat_ladder),
    )
    capital = _number(item, "start_capital", base.start_capital)
    return replace(
        base,
        overrides=overrides,
        sleeve_weight=_number(item, "sleeve_weight", base.sleeve_weight),
        slot_order=str(item.get("slot_order", base.slot_order)),
        entry_fill_rule=str(item.get("entry_fill", base.entry_fill_rule)),
        ambiguous=str(item.get("ambiguous", base.ambiguous)),
        suspension_sessions=_count(item, "suspension_sessions", base.suspension_sessions) or 0,
        regime_mult=str(item.get("regime_mult", base.regime_mult)),
        start_capital=base.start_capital if capital is None else capital,
        entry_bars=str(item.get("entry_bars", base.entry_bars)),
        volume_pace=str(item.get("volume_pace", base.volume_pace)),
    )


def _texts(
    item: Mapping[str, object], key: str, parse: Callable[[str], tuple[str, Any]]
) -> tuple[tuple[str, Any], ...]:
    """The item's list of `path=value` strings under `key`, each parsed by `parse`."""
    texts = item.get(key, [])
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise BacktestError("backtest_sweep_bad_variants")
    try:
        return tuple(parse(text) for text in texts)
    except OverrideError as exc:
        raise BacktestError(exc.code) from None


def _number(item: Mapping[str, object], key: str, default: Decimal | None) -> Decimal | None:
    if key not in item:
        return default
    found = exact(item[key], floats=True)
    if found is None:
        raise BacktestError("backtest_sweep_bad_variants")
    return found


def _names(
    item: Mapping[str, object], key: str, default: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    if key not in item:
        return default
    found = item[key]
    if not isinstance(found, list) or not found or not all(isinstance(n, str) for n in found):
        raise BacktestError("backtest_sweep_bad_variants")
    return tuple(found)


def _flag(item: Mapping[str, object], key: str, default: bool) -> bool:
    found = item.get(key, default)
    if not isinstance(found, bool):
        raise BacktestError("backtest_sweep_bad_variants")
    return found


def _count(item: Mapping[str, object], key: str, default: int | None) -> int | None:
    if key not in item:
        return default
    found = item[key]
    if isinstance(found, bool) or not isinstance(found, int):
        raise BacktestError("backtest_sweep_bad_variants")
    return found


def sweep_bot(
    session: Session,
    store: DataStore,
    request: RunRequest,
    sweep: Sweep,
    *,
    root: Path,
    after: Callable[[RunOutcome], None] = lambda outcome: None,
    budget: TrialBudget | None = None,
) -> list[RunOutcome]:
    """Run each variant of `request` (its conventions in place of the request's) over one load of
    the market, in order, each published as its run folder with the sweep's id and name, its
    label, its base (an earlier label resolved to the run it made) and change, so a later run
    sees the earlier ones in its DSR trial count; `after` takes each variant's outcome as it is
    decided, in order, refused ones too (the command line prints it there). A variant refused
    before it runs (a bad override, a missing weight, a hold-out without the run it validates
    or failing its guards, `discipline.check_hold_out`, an undeclared bundle against a base
    named by run id) is skipped with its code; the request-wide refusals, a variant changing
    more than one knob against a base of its file (`check_one_change`), and a sweep whose
    variants read different regime indices, and a variant whose override path names nothing
    (`check_paths`), raise `BacktestError` before any run.

    With a `budget` (the strategy's criteria, `TrialBudget`), an in-sample variant whose
    resolved config would be a new trial once the family's own trials (`trials.counts`, prior
    ones excluded) have reached it is refused `backtest_sweep_trial_budget`, and each outcome
    carries the family's own trial count after it (`RunOutcome.trials_own`); a budget for
    another strategy or universe is refused `backtest_sweep_criteria_scope`."""
    target = _target(session, request)
    check_paths(sweep, target.config, target.policy)
    check_one_change(sweep, target.config.to_json())
    if budget is not None and (budget.strategy, budget.universe) != (
        target.strategy,
        target.universe,
    ):
        raise BacktestError("backtest_sweep_criteria_scope")
    if request.labels.split != IN_SAMPLE:
        budget = None  # a hold-out adds no trial
    family = trials.family_key(target.strategy, target.universe, request.start, request.end)
    plans: list[tuple[Variant, RunSpec | str]] = []
    for variant in sweep.variants:
        try:
            spec = _spec(target, request, variant.conventions)
            _guard_variant(root, request, variant, spec, target)
            plans.append((variant, spec))
        except BacktestError as exc:
            plans.append((variant, exc.code))
    runnable = [spec for _, spec in plans if isinstance(spec, RunSpec)]
    market = (
        _load(
            session, store, target.universe_version_id, (request.start, request.end), runnable, root
        )
        if runnable
        else None
    )
    if market is not None:  # a capped variant is refused alone, never its uncapped siblings
        plans = [
            (v, (_cap_refusal(s, market.audit) or s) if isinstance(s, RunSpec) else s)
            for v, s in plans
        ]
    made: dict[str, str] = {}
    out: list[RunOutcome] = []
    for variant, spec in plans:
        if budget is not None and isinstance(spec, RunSpec):
            spec = _budget_refusal(root, family, spec, budget.limit) or spec
        outcome = _sweep_run(target, request, sweep, variant, spec, made, market, root)
        if budget is not None:
            outcome = replace(outcome, trials_own=_own_trials(root, family))
        if outcome.run_id is not None:
            made[variant.label] = outcome.run_id
        out.append(outcome)
        after(outcome)
    return out


@dataclass(frozen=True)
class TrialBudget:
    """The trial budget a sweep keeps: its criteria block's strategy, universe and
    `trial_budget` (`criteria.Criteria`)."""

    strategy: str
    universe: str
    limit: int


def _own_trials(root: Path, family: str) -> int:
    total, prior = trials.counts(trials.read_registry(root), family)
    return total - prior


def _budget_refusal(root: Path, family: str, spec: RunSpec, limit: int) -> str | None:
    """`backtest_sweep_trial_budget` when `spec` would be a new trial past `limit`
    (`trials.over_budget`, read afresh: the sweep's earlier runs have joined the registry)."""
    digest = config_hash(resolved_of(spec))
    if trials.over_budget(trials.read_registry(root), family, digest, limit):
        return "backtest_sweep_trial_budget"
    return None


def _guard_variant(
    root: Path, request: RunRequest, variant: Variant, spec: RunSpec, target: _Target
) -> None:
    """A variant's guards before the sweep runs: against a base named by run id whose
    `config.json` records its knobs, more than one changed knob needs a declared bundle
    (`backtest_sweep_not_one_change`; a base of the file was checked by `parse_variants`); a
    hold-out variant's guards (`discipline.check_hold_out`)."""
    if variant.base is not None and folder.is_run_id(variant.base) and variant.bundle is None:
        base = folder.read_config(root, variant.base) or {}
        knobs = base.get("knobs")
        if (
            isinstance(knobs, dict)
            and len(
                changed_knobs(
                    _knobs(request, spec.conventions, target),
                    resolve_knobs(knobs, target.config.to_json()),
                )
            )
            > 1
        ):
            raise BacktestError("backtest_sweep_not_one_change")
    validates = variant.validates or request.labels.validates
    if request.labels.split == HOLD_OUT and validates is not None:
        check_hold_out(root, validates, resolved_of(spec), spec.start)


def _base_of(variant: Variant, sweep: Sweep, made: Mapping[str, str]) -> str | None:
    """The run id of a variant's base: the run an earlier variant of the file made (None when
    that variant was refused), or the run id it names."""
    if variant.base is None:
        return None
    if variant.base in {v.label for v in sweep.variants}:
        return made.get(variant.base)
    return variant.base


def _sweep_run(
    target: _Target,
    request: RunRequest,
    sweep: Sweep,
    variant: Variant,
    spec: RunSpec | str,
    made: Mapping[str, str],
    market: _Market | None,
    root: Path,
) -> RunOutcome:
    if isinstance(spec, str) or market is None:
        return RunOutcome(variant.label, None, "refused", str(spec))
    try:
        labels = Labels(
            sweep_id=sweep.sweep_id,
            sweep_name=sweep.sweep_name,
            variant=variant.label,
            base=_base_of(variant, sweep, made),
            change=variant.change,
            split=request.labels.split,
            validates=variant.validates or request.labels.validates,
            bundle=variant.bundle,
        )
        _guard_hold_out(root, spec, labels)  # again: an earlier variant may have held it out
    except BacktestError as exc:
        return RunOutcome(variant.label, None, "refused", exc.code)
    asked = {"args": list(request.args), "variant_item": dict(variant.item)}
    plan = _plan(request, spec, labels, asked, target)
    return _execute(target, plan, market, root=root, label=variant.label)


# --- rerun -------------------------------------------------------------------------------------


def _replayed(
    config: Mapping[str, Any], window: tuple[date, date] | None = None
) -> tuple[_Target, RunSpec]:
    """The target and spec a run's `config.json` records, exactly as it ran (the documents
    after their overrides), over its own period or `window`; `backtest_rerun_bad_config` for a
    file that is not one."""
    try:
        policy_block = config["risk_policy"]
        c = config["conventions"]
        weight = c["sleeve_weight"]
        period = config["period"]
        fills = config["fills"]
        start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
        if window is not None:
            start, end = window
        strategy_config = parse_config(config["strategy_config"])
        policy = PolicyVersion(
            uuid.UUID(policy_block["version_id"]),
            int(policy_block["version"]),
            utc_now(),
            None,
            parse_policy(policy_block["document"]),
        )
        slippage = exact(fills["slippage_bps"])
        table = {k: exact(v) for k, v in c["regime_table"].items()}
        if slippage is None or any(v is None for v in table.values()):
            raise BacktestError("backtest_rerun_bad_config")
        conventions = RunConventions(
            entry_fill_rule=c["entry_fill_rule"],
            slot_order=c["slot_order"],
            ambiguous=c["ambiguous"],
            suspension_sessions=int(c["suspension_sessions"]),
            sleeve_weight=None if weight is None else exact(weight),
            regime_mult=c["regime_mult"],
            start_capital=exact(c.get("start_capital", str(INITIAL_EQUITY))),
            entry_bars=c.get("entry_bars", ENTRY_BARS_5M),
            volume_pace=c.get("volume_pace", VOLUME_PACE_LIVE),
        )
        universe = config["universe"]
        co = tuple(_replayed_co(item) for item in config.get("co_sleeves", ()))
        target = _Target(
            uuid.UUID(config["strategy_version_id"]),
            uuid.UUID(config["strategy_id"]),
            strategy_config,
            policy,
            uuid.UUID(universe["universe_id"]),
            uuid.UUID(universe["universe_version_id"]),
            _fills(str(fills["cost_model"]), slippage, start),
            {k: v for k, v in table.items() if v is not None},
            str(config["strategy"]),
            str(universe["label"]),
            tuple(t for t, _ in co),
        )
        overlay = None if "overlay" not in config else replay_overlay(config["overlay"])
        spec = RunSpec(
            config=strategy_config,
            strategy_version_id=target.version_id,
            policy=policy,
            start=start,
            end=end,
            fills=target.fills,
            initial_equity=conventions.start_capital,
            for_promotion=bool(config["for_promotion"]),
            conventions=conventions,
            regime_table=target.regime_table,
            co_sleeves=tuple(c for _, c in co),
            overlay=overlay,
        )
    except (KeyError, TypeError, ValueError, ConfigError, PolicyError, OverlayError):
        raise BacktestError("backtest_rerun_bad_config") from None
    return target, spec


def _replayed_co(item: Mapping[str, Any]) -> tuple[_CoTarget, CoSleeve]:
    """A co-sleeve as `config.json` records it (`_portfolio_doc`): its config as it ran, so no
    override is applied again (the record keeps them for the knobs and the digest)."""
    config = parse_config(item["strategy_config"])
    table = {k: exact(v) for k, v in item["regime_table"].items()}
    weight = exact(item["weight"])
    if weight is None or any(v is None for v in table.values()):
        raise BacktestError("backtest_rerun_bad_config")
    found = {k: v for k, v in table.items() if v is not None}
    version_id = uuid.UUID(item["strategy_version_id"])
    target = _CoTarget(
        version_id, uuid.UUID(item["strategy_id"]), config, found, str(item["strategy"])
    )
    overrides = _recorded_overrides(item.get("overrides") or {})
    return target, CoSleeve(config, version_id, weight, found, overrides)


def _recorded_overrides(record: Mapping[str, Any]) -> Overrides:
    """The config overrides a co-sleeve's record names (`Overrides.record`: its sets and kept
    playbooks; a co-sleeve sets no policy)."""
    playbooks = record.get("playbooks")
    return Overrides(
        sets=tuple((s["path"], s["value"]) for s in record.get("set", ())),
        playbooks=None if playbooks is None else tuple(playbooks),
    )


def rerun(
    session: Session, store: DataStore, run_id: str, *, root: Path, args: Sequence[str] = ()
) -> RunOutcome:
    """Run again what a run's folder records (`config.json` alone: the strategy and policy
    documents as they ran, the universe version, the period, the fills and conventions) as a
    new run whose base is the old one, so its binding says whether it made the same trades.
    Refused `backtest_rerun_unknown` without that file and `backtest_rerun_snapshot_differs`
    when the market it loads is not the data the run read (`data_snapshot_id`); a hold-out run
    is not rerun once it has been seen (`backtest_hold_out_repeated`, `discipline`)."""
    config = folder.read_config(root, run_id)
    if config is None:
        raise BacktestError("backtest_rerun_unknown")
    target, spec = _replayed(config)
    old = config.get("labels") or {}
    if old.get("split") == HOLD_OUT and old.get("validates_run_id") is not None:
        # a hold-out is read once: its rerun is a second look (`backtest_hold_out_repeated`)
        check_hold_out(root, old["validates_run_id"], resolved_of(spec), spec.start)
    market = _load(session, store, target.universe_version_id, (spec.start, spec.end), [spec], root)
    if market.snapshot.get("content_digest") != config.get("data_snapshot_id"):
        raise BacktestError("backtest_rerun_snapshot_differs")
    if spec.overlay is not None and _etf_files(market.snapshot) != _etf_files(
        config.get("data_snapshot") or {}
    ):  # the overlay read other ETF files than the run did
        raise BacktestError("backtest_rerun_snapshot_differs")
    labels = Labels(
        variant=old.get("variant"),
        base=run_id,
        change=f"rerun of {run_id}",
        split=old.get("split") or IN_SAMPLE,
        validates=old.get("validates_run_id"),
        bundle=old.get("bundle"),
    )
    plan = _Plan(spec, labels, {"args": list(args), "rerun_of": run_id}, {}, _old_knobs(config))
    return _execute(target, plan, market, root=root)


def _old_knobs(config: Mapping[str, Any]) -> dict[str, str]:
    knobs = config.get("knobs")
    return dict(knobs) if isinstance(knobs, dict) else {}


def hold_out(
    session: Session,
    store: DataStore,
    run_id: str,
    window: tuple[date, date],
    *,
    root: Path,
    args: Sequence[str] = (),
) -> RunOutcome:
    """The hold-out of a candidate: its `config.json` replayed (`_replayed`, as `rerun`) over
    `window`, a hold-out run that validates it, in its sweep and with its variant label, change
    and knobs. Refused before any load by `discipline.check_hold_out` (a candidate that is not a
    completed in-sample run, a window that does not start after the candidate's, a second
    hold-out of the same config) and `backtest_rerun_unknown` without the candidate's file; the
    data is the store's now, not the candidate's snapshot (the window differs)."""
    config = folder.read_config(root, run_id)
    if config is None:
        raise BacktestError("backtest_rerun_unknown")
    target, spec = _replayed(config, window)
    old = config.get("labels") or {}
    labels = Labels(
        sweep_id=old.get("sweep_id"),
        sweep_name=old.get("sweep_name"),
        variant=old.get("variant"),
        change=old.get("change"),
        split=HOLD_OUT,
        validates=run_id,
        bundle=old.get("bundle"),
    )
    _guard_hold_out(root, spec, labels)
    market = _load(session, store, target.universe_version_id, window, [spec], root)
    candidate = folder.read_run(root, run_id) or {}
    overrides = (candidate.get("config") or {}).get("overrides") or {}  # the candidate's
    asked = {"args": list(args), "hold_out_of": run_id}
    plan = _Plan(spec, labels, asked, dict(overrides), _old_knobs(config))
    return _execute(target, plan, market, root=root)
