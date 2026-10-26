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
floors (`dsr.family_deflated_sharpe`), over the strategy's trials in the registry (a distinct
`resolved_hash` among its completed in-sample runs, this one included), with the variance of
their Sharpe ratios; `dsr_n_trials` records the count.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.paths import PROJECT_ROOT

from ..confighash import config_hash
from ..costs import (
    DEFAULT_COST_MODEL,
    DEFAULT_SLIPPAGE_BPS,
    BacktestFills,
    CostError,
    check_slippage,
    load_cost_schedule,
)
from ..data.index_history import NIFTY_500_TRI
from ..data.store import DataStore
from ..engine.portfolio.common import market_id_of
from ..engine.portfolio.regime_service import regime_profile, stored_regime_table
from ..engine.strategy.config import ConfigError, StrategyConfig, parse_config
from ..models import (
    HatBot,
    HatStrategy,
    HatStrategyVersion,
    HatUniverse,
    HatUniverseVersion,
)
from ..money import exact
from ..risk.policy import PolicyError, PolicyVersion, RiskPolicy, effective_version, parse_policy
from ..risk.policy_upkeep import load_versions
from ..time import utc_now
from . import folder, trials
from .data import MarketData, load_market
from .dsr import family_deflated_sharpe, trial_variance
from .evidence import RunMetrics, universe_live_version, universe_of
from .loop import run_backtest
from .overrides import OverrideError, Overrides, parse_policy_set, parse_set, read_policy_file
from .spec import (
    REGIME_MULT_POLICY,
    BacktestError,
    RunConventions,
    RunSpec,
    known_gaps,
    with_cooldown,
)
from .standard import FORMAT_VERSION, changed_paths

__all__ = [
    "HOLD_OUT",
    "IN_SAMPLE",
    "STRATEGY_LABELS",
    "Labels",
    "RunOutcome",
    "RunRequest",
    "Sweep",
    "Variant",
    "backtest_bot",
    "engine_commit",
    "parse_variants",
    "rerun",
    "resolved_of",
    "sweep_bot",
]

logger = logging.getLogger(__name__)

IN_SAMPLE, HOLD_OUT = "in_sample", "hold_out"
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
    one-line change against it, its split and, for a hold-out run, the run it validates."""

    sweep_id: str | None = None
    sweep_name: str | None = None
    variant: str | None = None
    base: str | None = None
    change: str | None = None
    split: str = IN_SAMPLE
    validates: str | None = None

    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise BacktestError("backtest_bad_split")
        if (self.split == HOLD_OUT) != (self.validates is not None):
            raise BacktestError("backtest_hold_out_validates")


@dataclass(frozen=True)
class RunRequest:
    """One backtest (`backtest_bot`): of a sleeve (`bot_id`: its strategy version, the live one
    unless `version_id` names another of the same strategy, on its universe), or, in research
    mode, of a strategy version (`version_id`) on a universe (`universe`, its id or its name
    among the strategy owner's universes) with no bot, so no broker account; the period, the
    fills (slippage and cost model), the cooldown replacing the policy's for a sweep, whether
    the run may evidence a version, its conventions and labels, and the command line's
    arguments (recorded in `config.json`)."""

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
        live = universe_live_version(session, universe_of(session, bot))
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
    strategy = session.get(HatStrategy, version.strategy_id)
    named = session.get(HatUniverse, universe.universe_id)
    return _Target(
        version.id,
        version.strategy_id,
        config,
        policy,
        universe.universe_id,
        universe.id,
        fills,
        table,
        _strategy_label("" if strategy is None else strategy.name),
        _universe_label(universe, "" if named is None else named.name),
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
    universe_version_id: uuid.UUID,
    period: tuple[date, date],
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
        universe_version_id=universe_version_id,
        index=indices.pop(),
        start=period[0],
        end=period[1],
        regime_inputs=any(s.conventions.regime_mult == REGIME_MULT_POLICY for s in specs),
    )


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
    return {
        "strategy": spec.config.to_json(),
        "risk_policy": spec.policy.policy.to_json(),
        "conventions": conventions,
    }


def _capital(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass(frozen=True)
class RunOutcome:
    """A run's id (None when it was refused before it ran), status, refusal or failure code,
    and its `run.json` document."""

    label: str | None
    run_id: str | None
    status: str
    code: str | None
    run: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _Plan:
    """One run to execute: its spec, labels (the base resolved to a run id), what
    `config.json` records of how it was asked for, and its overrides block."""

    spec: RunSpec
    labels: Labels
    asked: Mapping[str, Any]
    overrides: Mapping[str, Any]


def _config_doc(
    run_id: str, target: _Target, plan: _Plan, snapshot: Mapping[str, Any]
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
        },
        "labels": {
            "sweep_id": plan.labels.sweep_id,
            "sweep_name": plan.labels.sweep_name,
            "variant": plan.labels.variant,
            "base_run_id": plan.labels.base,
            "change": plan.labels.change,
            "split": plan.labels.split,
            "validates_run_id": plan.labels.validates,
        },
        "asked": dict(plan.asked),
        "data_snapshot_id": snapshot.get("content_digest"),
        "data_snapshot": dict(snapshot),  # what was read: the store, membership and its cover
    }


def _blocks(
    run_id: str,
    target: _Target,
    plan: _Plan,
    snapshot: Mapping[str, Any],
    root: Path,
    status: str,
    error: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """run.json's `identity`, `data` and `config` blocks."""
    spec, labels = plan.spec, plan.labels
    resolved = resolved_of(spec)
    base = None if labels.base is None else folder.read_run(root, labels.base)
    base_resolved = None if base is None else (base.get("config") or {}).get("resolved")
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
        "created_at": utc_now().isoformat(),
        "status": status,
        "error": error,
        "for_promotion": spec.for_promotion,
    }
    data = {
        "universe": target.universe,
        "data_snapshot_id": snapshot.get("content_digest"),
        "split": labels.split,
        "validates_run_id": labels.validates,
        "period_start": spec.start.isoformat(),
        "period_end": spec.end.isoformat(),
        "benchmark": BENCHMARK_LABEL,
    }
    config = {
        "overrides": changed_paths(resolved, base_resolved)
        if isinstance(base_resolved, dict)
        else dict(plan.overrides),
        "resolved": resolved,
        "resolved_hash": config_hash(resolved),
        "cost_model": spec.fills.schedule.version,
        "start_capital": _capital(spec.initial_equity),
        "rf_pct": 0,
    }
    return identity, data, config


def _execute(
    target: _Target,
    plan: _Plan,
    market: tuple[MarketData, dict[str, Any]],
    *,
    root: Path,
    label: str | None = None,
) -> RunOutcome:
    """Run one plan and publish its folder: complete, or failed with the engine's refusal code
    (`backtest_error` for anything else, chained); a stop (Ctrl-C) publishes `stopped_early`
    and goes on stopping. A completed in-sample run joins the trial registry."""
    data, snapshot = market
    run_id = str(uuid.uuid7())
    spec, labels = plan.spec, plan.labels
    config_doc = _config_doc(run_id, target, plan, snapshot)

    def inputs(status: str, error: str | None, **extra: Any) -> folder.RunInputs:  # noqa: ANN401
        identity, data_block, config = _blocks(run_id, target, plan, snapshot, root, status, error)
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
        folder.publish(root, run_id, doc, config_doc)
        return RunOutcome(label, run_id, status, code, doc)

    try:
        result = run_backtest(spec, data)
        resolved_hash = config_hash(resolved_of(spec))
        stats = RunMetrics.from_json(result.metrics).returns
        own = (resolved_hash, None if stats is None else stats.sr)
        count, sharpes = trials.family(
            trials.read_registry(root), target.strategy, own if labels.split == IN_SAMPLE else None
        )
        deflated = family_deflated_sharpe(stats, count, trial_variance(sharpes))
        built = inputs(
            COMPLETE,
            None,
            benchmark=data.index_closes.get(NIFTY_500_TRI, ()),
            base_trades=None if labels.base is None else folder.trade_keys(root, labels.base),
            dsr_prob=deflated.prob,
            dsr_n_trials=count,
        )
        doc, csvs = folder.build(run_id, result, built)
        folder.publish(root, run_id, doc, config_doc, csvs)
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
        trials.record_trial(root, target.strategy, resolved_hash, run_id, own[1])
    return RunOutcome(label, run_id, COMPLETE, None, doc)


def _plan(spec: RunSpec, labels: Labels, asked: Mapping[str, Any]) -> _Plan:
    return _Plan(spec, labels, asked, spec.conventions.overrides.record())


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
    market = _load(session, store, target.universe_version_id, (request.start, request.end), [spec])
    plan = _plan(spec, request.labels, {"args": list(request.args), "variant_item": None})
    return _execute(target, plan, market, root=root)


# --- sweeps ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One run of a sweep: a label, its conventions (the overrides among them), its base (a
    label earlier in the file or a run id; None: a baseline), the one-line change, the run a
    hold-out variant validates, and its item as the file wrote it."""

    label: str
    conventions: RunConventions
    base: str | None = None
    change: str | None = None
    validates: str | None = None
    item: Mapping[str, Any] = field(default_factory=dict)


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
        "set",
        "risk_pct",
        "max_positions",
        "max_per_industry",
        "flat_ladder",
        "policy",
        "policy_set",
        "sleeve_weight",
        "slot_order",
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
    `flat_ladder` (a boolean), `sleeve_weight` and `slot_order` (each replacing the base's).
    `backtest_sweep_bad_variants` for any other shape; a bad value raises the code its
    conventions or override raise."""
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
        if (
            based is not None
            and based not in {v.label for v in out}
            and not folder.is_run_id(based)
        ):
            raise BacktestError("backtest_sweep_bad_variants")  # a base the file has not run yet
        out.append(
            Variant(
                label,
                _conventions(item, base, read_policy),
                based,
                change,
                _text(item, "validates"),
                dict(item),
            )
        )
    if len({v.label for v in out}) != len(out):
        raise BacktestError("backtest_sweep_bad_variants")
    return Sweep(sweep_id, _text(doc, "sweep_name") or sweep_id, tuple(out))


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
        policy=policy,
        policy_sets=(*base.overrides.policy_sets, *policy_sets),
        risk_pct=_number(item, "risk_pct", base.overrides.risk_pct),
        max_positions=_count(item, "max_positions", base.overrides.max_positions),
        max_per_industry=_count(item, "max_per_industry", base.overrides.max_per_industry),
        flat_ladder=_flag(item, "flat_ladder", base.overrides.flat_ladder),
    )
    return replace(
        base,
        overrides=overrides,
        sleeve_weight=_number(item, "sleeve_weight", base.sleeve_weight),
        slot_order=str(item.get("slot_order", base.slot_order)),
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
) -> list[RunOutcome]:
    """Run each variant of `request` (its conventions in place of the request's) over one load of
    the market, in order, each published as its run folder with the sweep's id and name, its
    label, its base (an earlier label resolved to the run it made) and change, so a later run
    sees the earlier ones in its DSR trial count; `after` takes each variant's outcome as it is
    decided, in order, refused ones too (the command line prints it there). A variant refused
    before it runs (a bad override, a missing weight, a hold-out without the run it validates)
    is skipped with its code; the request-wide refusals, and a sweep whose variants read
    different regime indices, raise `BacktestError` before any run."""
    target = _target(session, request)
    plans: list[tuple[Variant, RunSpec | str]] = []
    for variant in sweep.variants:
        try:
            plans.append((variant, _spec(target, request, variant.conventions)))
        except BacktestError as exc:
            plans.append((variant, exc.code))
    runnable = [spec for _, spec in plans if isinstance(spec, RunSpec)]
    market = (
        _load(session, store, target.universe_version_id, (request.start, request.end), runnable)
        if runnable
        else None
    )
    made: dict[str, str] = {}
    out: list[RunOutcome] = []
    for variant, spec in plans:
        outcome = _sweep_run(target, request, sweep, variant, spec, made, market, root)
        if outcome.run_id is not None:
            made[variant.label] = outcome.run_id
        out.append(outcome)
        after(outcome)
    return out


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
    market: tuple[MarketData, dict[str, Any]] | None,
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
        )
    except BacktestError as exc:
        return RunOutcome(variant.label, None, "refused", exc.code)
    asked = {"args": list(request.args), "variant_item": dict(variant.item)}
    return _execute(target, _plan(spec, labels, asked), market, root=root, label=variant.label)


# --- rerun -------------------------------------------------------------------------------------


def _replayed(config: Mapping[str, Any]) -> tuple[_Target, RunSpec]:
    """The target and spec a run's `config.json` records, exactly as it ran (the documents
    after their overrides); `backtest_rerun_bad_config` for a file that is not one."""
    try:
        policy_block = config["risk_policy"]
        c = config["conventions"]
        weight = c["sleeve_weight"]
        period = config["period"]
        fills = config["fills"]
        start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
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
        )
        universe = config["universe"]
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
        )
        spec = RunSpec(
            config=strategy_config,
            strategy_version_id=target.version_id,
            policy=policy,
            start=start,
            end=end,
            fills=target.fills,
            for_promotion=bool(config["for_promotion"]),
            conventions=conventions,
            regime_table=target.regime_table,
        )
    except (KeyError, TypeError, ValueError, ConfigError, PolicyError):
        raise BacktestError("backtest_rerun_bad_config") from None
    return target, spec


def rerun(
    session: Session, store: DataStore, run_id: str, *, root: Path, args: Sequence[str] = ()
) -> RunOutcome:
    """Run again what a run's folder records (`config.json` alone: the strategy and policy
    documents as they ran, the universe version, the period, the fills and conventions) as a
    new run whose base is the old one, so its binding says whether it made the same trades.
    Refused `backtest_rerun_unknown` without that file and `backtest_rerun_snapshot_differs`
    when the market it loads is not the data the run read (`data_snapshot_id`)."""
    config = folder.read_config(root, run_id)
    if config is None:
        raise BacktestError("backtest_rerun_unknown")
    target, spec = _replayed(config)
    market = _load(session, store, target.universe_version_id, (spec.start, spec.end), [spec])
    if market[1].get("content_digest") != config.get("data_snapshot_id"):
        raise BacktestError("backtest_rerun_snapshot_differs")
    old = config.get("labels") or {}
    labels = Labels(
        variant=old.get("variant"),
        base=run_id,
        change=f"rerun of {run_id}",
        split=old.get("split") or IN_SAMPLE,
        validates=old.get("validates_run_id"),
    )
    plan = _Plan(spec, labels, {"args": list(args), "rerun_of": run_id}, {})
    return _execute(target, plan, market, root=root)
