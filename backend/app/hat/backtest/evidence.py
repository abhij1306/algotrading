"""The evidence a backtest gives (HAT-BT-060): what auto mode, the sleeve lifecycle, a version's
promotion and the risk-increase gate read of the run folders (`HAT_DATA_ROOT/backtests/runs/`).

A completed run's folder holds `evidence.json` beside the Result Standard's files
(`evidence_doc`, written by `runs`): the run's metrics as `report.compute_metrics` gave them,
completed with its DSR, PBO and completion time, its identity (strategy, version, universe and
member set) and its confidence buckets. `RunMetrics.from_json` is the one reader of the facts any
gate reads: every reader here and `publish_version` goes through it, so a key renamed on the
write side fails one parse (and a plain test of `compute_metrics`) instead of silently making a
fact absent. A fact missing or malformed reads as None and fails its condition (fail closed); a
folder whose `run.json` does not say complete, whose run audit (`audit.json`) failed or is
missing, or without `evidence.json`, is no evidence (`audit_admits`).

The sleeve's evidence is its newest completed run, by completion time, of its live version on its
universe, flagged for promotion (PR 62, O5) and run under the cooldown in force (HAT-RISK-036).
`RunSpec` refuses to record a promotion run under any convention live does not trade by (slot
order, fill rule, regime rule, ...), so the flag alone says the run sized and filled as live.

The deflated Sharpe probability depends on the family's trial count N and the variance V of its
trial Sharpe ratios (the run's family in the trial registry, `trials`), which grow after a run is
recorded. A run's stored DSR is the value at its completion; the evidence is recomputed from the
run's return moments at the family's current N and V (`run_dsr`), so an old run never passes the
gate on a stale count, always through the family floors of `dsr.family_deflated_sharpe`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configs.records import Bot, StrategyVersion
from ..configs.unit import configs_of, data_root_of
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
from ..engine.portfolio.lifecycle import (
    DEFAULT_DISCOUNT,
    BacktestFacts,
    ambiguous_gate,
    discounted_benchmark,
    span_gate,
)
from ..engine.strategy.config import ConfigError, Playbook, StrategyConfig, parse_config
from ..models import (
    HatPosition,
    HatSignal,
    HatTradeResult,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..money import exact
from ..risk.policy import (
    PolicyError,
    PolicyVersion,
    TradeOutcome,
    build_gate_evidence,
    effective_version,
)
from ..risk.policy_upkeep import load_versions
from ..time import as_utc, utc_now
from . import run_files, trials
from .dsr import Deflated, ReturnStats, family_deflated_sharpe, trial_variance
from .spec import GAP_PLAYBOOKS, KNOWN_GAPS, membership_key

__all__ = [
    "COMPLETED",
    "DSR_DIGITS",
    "AUDIT_ADMITS",
    "audit_admits",
    "EvidenceRun",
    "RunMetrics",
    "config_playbooks",
    "backtest_evidence",
    "evidence_doc",
    "evidence_run",
    "family_key",
    "gate_evidence",
    "history_holds",
    "read_evidence",
    "run_dsr",
    "run_playbooks",
    "signal_confidence",
    "unbacktested",
    "unbacktested_trades",
    "universe_live_version",
    "universe_of",
]

COMPLETED = "complete"  # a run folder's `identity.status` once it completed (`folder.COMPLETE`)
DSR_DIGITS = 6  # the places a DSR or PBO probability is read and stored at
AUDIT_ADMITS = ("pass", "warn")  # the run audit statuses a run is evidence under


# --- the stored metrics -----------------------------------------------------------------------


def _count(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _day(raw: object) -> date | None:
    try:
        return date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _instant(raw: object) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else None
    except ValueError:
        return None


def _decimal(raw: object) -> Decimal | None:
    """A finite Decimal written as text; None for anything else (a float never reads as one)."""
    return exact(raw) if isinstance(raw, str) else None


def _gaps(raw: object) -> tuple[str, ...] | None:
    if isinstance(raw, list) and all(isinstance(code, str) for code in raw):
        return tuple(raw)
    return None


def _pbo(raw: object) -> Decimal | None:
    """The probability of a stored PBO block (`runs.family_pbo`); None when it has none."""
    return _decimal(raw.get("prob")) if isinstance(raw, Mapping) else None


def _returns(raw: object) -> ReturnStats | None:
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


@dataclass(frozen=True)
class RunMetrics:
    """The facts of a run's stored metrics that a gate or the evidence reads, typed. The field
    names are the JSON keys `report.compute_metrics` writes (`returns` is its return moments,
    `known_gaps` the codes of `spec.KNOWN_GAPS`), besides `completed_at`, which
    `evidence_doc` adds. The rest of the report (figures, conventions, DSR, PBO) is read by
    people, not by a gate, and stays JSON."""

    trades: int | None = None
    expectancy_r: Decimal | None = None
    first_session: date | None = None
    last_session: date | None = None
    held_sessions: int | None = None
    ambiguous_days: int | None = None
    known_gaps: tuple[str, ...] | None = None
    sleeve_weight: Decimal | None = None
    regime_mult: str | None = None
    cooldown_sessions: int | None = None
    for_promotion: bool = False
    returns: ReturnStats | None = None
    initial_equity: Decimal | None = None
    pbo: Decimal | None = None  # the family's PBO at the run's completion (None: unscored)
    completed_at: datetime | None = None

    @staticmethod
    def from_json(raw: object) -> RunMetrics:
        """Parse stored metrics: a fact missing or malformed is None (`for_promotion` is True
        only when stored as true), and anything but an object reads as no facts at all."""
        metrics: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        regime = metrics.get("regime_mult")
        return RunMetrics(
            trades=_count(metrics.get("trades")),
            expectancy_r=_decimal(metrics.get("expectancy_r")),
            first_session=_day(metrics.get("first_session")),
            last_session=_day(metrics.get("last_session")),
            held_sessions=_count(metrics.get("held_sessions")),
            ambiguous_days=_count(metrics.get("ambiguous_days")),
            known_gaps=_gaps(metrics.get("known_gaps")),
            sleeve_weight=_decimal(metrics.get("sleeve_weight")),
            regime_mult=regime if isinstance(regime, str) else None,
            cooldown_sessions=_count(metrics.get("cooldown_sessions")),
            for_promotion=metrics.get("for_promotion") is True,
            returns=_returns(metrics.get("returns")),
            initial_equity=_decimal(metrics.get("initial_equity")),
            pbo=_pbo(metrics.get("pbo")),
            completed_at=_instant(metrics.get("completed_at")),
        )

    def gapped(self, playbooks: Iterable[Playbook]) -> frozenset[str]:
        """The names of the `playbooks` the run's known gaps name (`GAP_PLAYBOOKS`); a run
        without a readable gap list may have any gap, so every one of them counts as named."""
        if self.known_gaps is None:
            return frozenset(p.name for p in playbooks)
        return _named(self.known_gaps, playbooks)


def _named(codes: Iterable[str], playbooks: Iterable[Playbook]) -> frozenset[str]:
    """The names of the `playbooks` one of the gap `codes` names (`GAP_PLAYBOOKS`)."""
    names = [GAP_PLAYBOOKS[code] for code in codes if code in GAP_PLAYBOOKS]
    return frozenset(p.name for p in playbooks if any(named(p) for named in names))


def unbacktested(metrics: RunMetrics | None, playbooks: Sequence[Playbook]) -> frozenset[str]:
    """The names of the `playbooks` whose live and shadow trades are left out when they are
    compared with a run (PR 40): those its known gaps name, as a run cannot have their trades;
    with no run or no readable gap list, those every daily-bar run's gaps name (`KNOWN_GAPS`:
    the pre-open ones, which drop out once a run no longer names them, PR 70)."""
    daily_bar = _named(KNOWN_GAPS, playbooks)
    if metrics is None or metrics.known_gaps is None:
        return daily_bar
    return daily_bar & _named(metrics.known_gaps, playbooks)


# --- the run folders' evidence ----------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRun:
    """What a completed run folder's `evidence.json` says: the run, what it ran on (its
    strategy's trial family, version, universe and member set), its metrics and its confidence
    buckets by (playbook, band)."""

    run_id: str
    strategy: str
    resolved_hash: str | None
    trial_family: str | None  # its key in the trial registry (`trials.family_of`)
    strategy_id: uuid.UUID | None
    strategy_version_id: uuid.UUID | None
    universe_id: uuid.UUID | None
    membership_key: object
    metrics: RunMetrics
    buckets: Mapping[tuple[str, str], BucketStats]


def _bucket_json(playbook: str, band: str, stats: BucketStats) -> dict[str, Any]:
    def text(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "playbook": playbook,
        "band": band,
        "trades": stats.trades,
        "mean_r": str(stats.mean_r),
        "ci90_low": text(stats.ci90_low),
        "ci90_high": text(stats.ci90_high),
        "win_rate": str(stats.win_rate),
    }


def evidence_doc(
    *,
    run_id: str,
    strategy: str,
    resolved_hash: str,
    strategy_id: uuid.UUID,
    strategy_version_id: uuid.UUID,
    universe_id: uuid.UUID,
    membership: object,
    metrics: Mapping[str, Any],
    trades: Iterable[tuple[str, object, Decimal]],
) -> dict[str, Any]:
    """A completed run's `evidence.json`: its identity, its `metrics` (already completed with
    the DSR, PBO and completion time) and the buckets of its closed `trades`, each given as
    (playbook, setup score, R multiple) (D132)."""
    return {
        "run_id": run_id,
        "strategy": strategy,
        "resolved_hash": resolved_hash,
        "strategy_id": str(strategy_id),
        "strategy_version_id": str(strategy_version_id),
        "universe_id": str(universe_id),
        "membership_key": membership,
        "metrics": dict(metrics),
        "buckets": [_bucket_json(p, b, s) for (p, b), s in buckets(trades).items()],
    }


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _buckets(raw: object) -> dict[tuple[str, str], BucketStats]:
    found: dict[tuple[str, str], BucketStats] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        trades, mean, rate = _count(item.get("trades")), item.get("mean_r"), item.get("win_rate")
        mean_r, win_rate = _decimal(mean), _decimal(rate)
        playbook, band = item.get("playbook"), item.get("band")
        if (
            trades is None
            or mean_r is None
            or win_rate is None
            or not isinstance(playbook, str)
            or not isinstance(band, str)
        ):
            continue
        found[(playbook, band)] = BucketStats(
            trades,
            mean_r,
            _decimal(item.get("ci90_low")),
            _decimal(item.get("ci90_high")),
            win_rate,
        )
    return found


# A published run folder never changes (`folder.publish` renames it in whole), so what was read
# of it is kept.
_READ: dict[tuple[str, str], EvidenceRun | None] = {}
_READ_LOCK = threading.Lock()


def audit_admits(audit: Mapping[str, Any] | None) -> bool:
    """A run is evidence only when its run audit (`audit.json`, `run_audit`) passed or warned: a
    failed audit, or none (a run from before the audits), is not (docs/backtests/process.md,
    rule 5). A warning is the reviewer's to explain, not the gate's."""
    return audit is not None and audit.get("status") in AUDIT_ADMITS


def read_evidence(root: Path, run_id: str) -> EvidenceRun | None:
    """The evidence of the completed run `run_id` under `root`; None for an unknown run id, a
    run that did not complete (its `run.json` status), one its `audit.json` does not admit
    (`audit_admits`) or one without a readable `evidence.json`."""
    key = (str(root), run_id)
    with _READ_LOCK:
        if key in _READ:
            return _READ[key]
    run = run_files.read_run(root, run_id)
    if run is None:  # no folder yet (it may still be published) or none readable: not cached
        return None
    found = _read_evidence(root, run_id, run)
    with _READ_LOCK:  # a published folder is whole and final: its answer, None too, is kept
        _READ[key] = found
    return found


def _read_evidence(root: Path, run_id: str, run: Mapping[str, Any]) -> EvidenceRun | None:
    if (run.get("identity") or {}).get("status") != COMPLETED:
        return None
    if not audit_admits(run_files.read_doc(root, run_id, "audit")):
        return None
    doc = run_files.read_doc(root, run_id, "evidence")
    if doc is None or doc.get("run_id") != run_id:
        return None
    strategy, resolved = doc.get("strategy"), doc.get("resolved_hash")
    validates = (run.get("data") or {}).get("validates_run_id")
    validated = run_files.read_run(root, validates) if isinstance(validates, str) else None
    return EvidenceRun(
        run_id=run_id,
        strategy=strategy if isinstance(strategy, str) else "",
        resolved_hash=resolved if isinstance(resolved, str) else None,
        trial_family=trials.family_of(run, validated),
        strategy_id=_uuid(doc.get("strategy_id")),
        strategy_version_id=_uuid(doc.get("strategy_version_id")),
        universe_id=_uuid(doc.get("universe_id")),
        membership_key=doc.get("membership_key"),
        metrics=RunMetrics.from_json(doc.get("metrics")),
        buckets=_buckets(doc.get("buckets")),
    )


def completed_runs(root: Path) -> list[EvidenceRun]:
    """Every completed run folder with its evidence, oldest first."""
    base = run_files.runs_dir(root)
    if not base.is_dir():
        return []
    names = sorted(p.name for p in base.iterdir() if run_files.is_run_id(p.name))
    return [run for run in (read_evidence(root, n) for n in names) if run is not None]


# --- the run a sleeve's evidence reads ----------------------------------------------------------


def family_key(strategy_id: uuid.UUID, universe_id: uuid.UUID) -> str:
    """The trial family of a strategy on a universe (HAT-BT-031): equal for two runs iff they
    share both, so a new version never escapes the trial penalty."""
    return f"{uuid.UUID(str(strategy_id))}:{uuid.UUID(str(universe_id))}"


def universe_of(session: Session, bot: Bot) -> uuid.UUID | None:
    """The universe of the bot's pinned universe version (None without one)."""
    if bot.universe_version_id is None:
        return None
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return None if version is None else version.universe_id


def universe_live_version(
    session: Session, universe_id: uuid.UUID | None
) -> HatUniverseVersion | None:
    """The universe's live version (None without a universe or a live version): the members a
    sleeve's scans and backtests read."""
    if universe_id is None:
        return None
    return session.scalars(
        select(HatUniverseVersion).where(
            HatUniverseVersion.universe_id == universe_id, HatUniverseVersion.status == "live"
        )
    ).first()


def _cooldown_of(policy: PolicyVersion | None) -> int:
    """The `cooldown_sessions` of a policy version (0 without one)."""
    return 0 if policy is None else policy.policy.cooldown_sessions


def evidence_run(
    session: Session, bot: Bot, cooldown: int, at: datetime | None = None
) -> EvidenceRun | None:
    """The sleeve's evidence run: the newest completed run folder (by completion time, the run
    id breaking a tie) of the bot's live version on the bot's universe, over the member set
    its live universe version defines now (the snapshot's `membership.universe_key`, so a run
    over another index, other includes or excludes, or today's list is not), flagged for
    promotion and run under `cooldown`, the cooldown in force when the evidence is read
    (HAT-RISK-036): a `--cooldown-sessions` sweep, or a run made before the cooldown changed,
    traded another rule. A run without a readable completion time is never the evidence. Given
    `at`, only a run completed at or before it is (PR 71): a signal processed late never reads
    results that did not exist when it triggered. None without a data root on the session."""
    root = data_root_of(session)
    universe_id = universe_of(session, bot)
    live = universe_live_version(session, universe_id)
    key = None if live is None else membership_key(live.definition)
    if root is None or bot.live_version_id is None or universe_id is None or key is None:
        return None
    cutoff = None if at is None else as_utc(at)
    found: list[tuple[datetime, str, EvidenceRun]] = []
    for run in completed_runs(root):
        metrics = run.metrics
        done = metrics.completed_at
        if (
            run.strategy_version_id == bot.live_version_id
            and run.strategy_id == bot.strategy_id
            and run.universe_id == universe_id
            and done is not None
            and run.membership_key == key
            and (cutoff is None or done <= cutoff)
            and metrics.for_promotion
            and metrics.cooldown_sessions == cooldown
        ):
            found.append((done, run.run_id, run))
    if not found:
        return None
    return max(found, key=lambda item: (item[0], item[1]))[2]


def run_dsr(root: Path, run: EvidenceRun) -> tuple[Deflated, int, float]:
    """A completed run's deflated Sharpe probability now, with its trial family's count in the
    registry (`trials`, the run's own counted once; a run that names no family counts only
    itself) and the variance of their Sharpe ratios, through the family floors
    (`family_deflated_sharpe`)."""
    stats = run.metrics.returns
    own = None if run.resolved_hash is None else (run.resolved_hash, stats and stats.sr)
    registry = {} if run.trial_family is None else trials.read_registry(root)
    count, sharpes = trials.family(registry, run.trial_family or "", own)
    variance = trial_variance(sharpes)
    return family_deflated_sharpe(stats, count, variance), count, variance


def _config_of(version: StrategyVersion | None) -> StrategyConfig | None:
    """The version's config, None when it has none that parses (its playbooks are then
    unknown, and the gate fails closed)."""
    if version is None:
        return None
    try:
        return parse_config(version.config)
    except ConfigError:
        return None


def config_playbooks(raw: object) -> tuple[Playbook, ...]:
    """The playbooks a strategy config document enables (none when it does not parse)."""
    try:
        return tuple(parse_config(raw).playbooks)
    except ConfigError:
        return ()


def run_playbooks(session: Session, run: EvidenceRun) -> tuple[Playbook, ...]:
    """The playbooks the run's strategy version enables (none when its config does not parse)."""
    config = _config_of(configs_of(session).get(StrategyVersion, run.strategy_version_id))
    return () if config is None else tuple(config.playbooks)


def _strategy_playbooks(session: Session, bot: Bot) -> dict[uuid.UUID, tuple[Playbook, ...]]:
    """The playbooks of each version of the bot's strategy whose config parses, by version id: a
    trade of the bot may come from any of them, and is judged by its own version's."""
    versions = configs_of(session).all(StrategyVersion, lambda v: v.strategy_id == bot.strategy_id)
    parsed = {v.id: _config_of(v) for v in versions}
    return {vid: tuple(c.playbooks) for vid, c in parsed.items() if c is not None}


def unbacktested_trades(
    metrics: RunMetrics | None, versions: Mapping[uuid.UUID, Sequence[Playbook]]
) -> frozenset[tuple[uuid.UUID, str]]:
    """The (strategy version, playbook) pairs whose trades are left out when they are compared
    with a run: `unbacktested` of each version's own playbooks, so a name that is pre-open in one
    version leaves out only that version's trades of it."""
    return frozenset(
        (version_id, name)
        for version_id, playbooks in versions.items()
        for name in unbacktested(metrics, playbooks)
    )


def history_holds(metrics: RunMetrics, playbooks: Sequence[Playbook]) -> bool:
    """The run's history passes the lifecycle's span, gap and ambiguity conditions
    (HAT-PORT-061, QA8): five years of sessions, no known gap naming one of the version's
    `playbooks` (and the playbooks known), and ambiguous days within the cap."""
    return (
        span_gate(metrics.first_session, metrics.last_session)
        and bool(playbooks)
        and not metrics.gapped(playbooks)
        and ambiguous_gate(metrics.ambiguous_days, metrics.held_sessions or 0)
    )


# --- what the gates read ----------------------------------------------------------------------


def backtest_evidence(session: Session, bot: Bot) -> BacktestFacts:
    """The sleeve's backtest evidence (HAT-BT-060): its evidence run (`evidence_run`), with the
    DSR recomputed at the family's current trial count and Sharpe variance; since PR 69 also what
    the rest of the gate reads (HAT-PORT-061): the run's first and last session, ambiguous days
    and held sessions, the playbooks its known gaps name (`GAP_PLAYBOOKS`) and those the version
    enables, and its sleeve weight and regime rule beside the base weight of the sleeve in the
    policy in force. A fact the run did not record is absent, and fails its condition. No run
    gives no evidence, so Research -> Backtested stays closed. The trades of the (version,
    playbook) pairs `unbacktested_trades` names are not compared with it (PR 40)."""
    in_force = effective_version(load_versions(session, bot.user_id), utc_now())
    run = evidence_run(session, bot, _cooldown_of(in_force))
    traded = _strategy_playbooks(session, bot)
    root = data_root_of(session)
    if run is None or root is None:
        return BacktestFacts(unbacktested=unbacktested_trades(None, traded))
    metrics = run.metrics
    deflated, count, _ = run_dsr(root, run)
    config = _config_of(configs_of(session).get(StrategyVersion, run.strategy_version_id))
    playbooks = run_playbooks(session, run)
    base = (
        None
        if in_force is None or config is None
        else in_force.policy.base_weight.get(sleeve_key(config.bot))
    )
    return BacktestFacts(
        dsr_prob=exact(deflated.prob, floats=True, places=DSR_DIGITS),
        trades=metrics.trades or 0,
        expectancy=metrics.expectancy_r,
        trials=count,
        pbo=metrics.pbo,
        first_session=metrics.first_session,
        last_session=metrics.last_session,
        ambiguous_days=metrics.ambiguous_days,
        held_sessions=metrics.held_sessions or 0,
        gapped_playbooks=metrics.gapped(playbooks),
        playbooks=frozenset(p.name for p in playbooks),
        sleeve_weight=metrics.sleeve_weight,
        base_weight=base,
        regime_mult=metrics.regime_mult,
        unbacktested=unbacktested_trades(metrics, traded),
    )


def signal_confidence(
    session: Session,
    bot: Bot,
    playbook: str,
    instrument_id: uuid.UUID,
    trade_date: date,
    at: datetime,
) -> Confidence:
    """The confidence label of a signal of `bot` (D132, HAT-EXEC-015): the bucket of its playbook
    and its watchlist row's setup-score band in the run `backtest_evidence` reads, among those
    completed by `at`. Low when there is no such run, when the run's known gaps name the
    playbook (the pre-open ones until they are backtested on 5-minute bars, PR 70), when the
    signal has no single score in a band, or when the bucket is empty; otherwise
    `confidence.label_of` decides. An unreadable policy history gives low too: the run under the
    cooldown in force cannot be told."""
    try:
        in_force = effective_version(load_versions(session, bot.user_id), at)
    except PolicyError:
        return Confidence.low(NO_POLICY, playbook=playbook)
    run = evidence_run(session, bot, _cooldown_of(in_force), at)
    if run is None:
        return Confidence.low(NO_RUN, playbook=playbook)
    metrics, run_id = run.metrics, run.run_id
    config = _config_of(configs_of(session).get(StrategyVersion, run.strategy_version_id))
    # an unreadable gap list names every playbook, the signal's own included
    if config is None or metrics.known_gaps is None or playbook in metrics.gapped(config.playbooks):
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
    stats = run.buckets.get((playbook, band))
    score = min(scores)
    return confidence_of(stats, run_id=run_id, playbook=playbook, band=band, score=score)


def gate_evidence(
    session: Session, user_id: uuid.UUID, at: datetime, discount: Decimal = DEFAULT_DISCOUNT
) -> dict[str, Any] | None:
    """The `gate_evidence` a risk increase requested at `at` is checked with (HAT-GOV-091): the
    trades of the user's sleeves closed since the policy version in force at `at` took effect
    (every closed trade when none is in force; shadow ones are ignored by
    `build_gate_evidence`), so each step up needs its own evidence, against the discounted
    backtest expectancy, the trade-weighted mean over the sleeves' evidence runs. Only what
    existed at `at` is read (C23): the evidence runs completed and the trades closed by then, so
    a gate replayed later sees what the request saw. Every evidence run with trades must also
    pass the lifecycle's span, gap and ambiguity conditions (QA8, `history_holds`): one that
    fails gives no evidence, and the increase is refused. None when no
    sleeve has one: the increase gate then refuses. A sleeve's trades of a playbook its run
    cannot have (`unbacktested`: the pre-open ones until PR 70) are not counted (PR 40)."""
    bots = configs_of(session).all(Bot, lambda b: b.user_id == user_id)
    in_force = effective_version(load_versions(session, user_id), at)
    weighted = Decimal(0)
    count = 0
    excluded: dict[uuid.UUID, frozenset[tuple[uuid.UUID, str]]] = {}
    for bot in bots:
        found = evidence_run(session, bot, _cooldown_of(in_force), at)
        metrics = None if found is None else found.metrics
        excluded[bot.id] = unbacktested_trades(metrics, _strategy_playbooks(session, bot))
        if found is None or metrics is None or metrics.expectancy_r is None:
            continue
        if metrics.trades is None or metrics.trades <= 0:
            continue
        if not history_holds(metrics, run_playbooks(session, found)):
            return None
        weighted += metrics.expectancy_r * metrics.trades
        count += metrics.trades
    if count == 0:
        return None
    query = (
        select(
            HatTradeResult.r_multiple,
            HatPosition.is_virtual,
            HatPosition.bot_id,
            HatSignal.strategy_version_id,
            HatSignal.playbook,
        )
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id.in_([b.id for b in bots]),
            HatPosition.closed_at <= as_utc(at),
        )
        .order_by(HatPosition.closed_at, HatPosition.id)
    )
    if in_force is not None:
        query = query.where(HatPosition.closed_at >= as_utc(in_force.effective_from))
    rows = session.execute(query).all()
    outcomes = [
        TradeOutcome(Decimal(r), bool(virtual))
        for r, virtual, bot_id, version_id, playbook in rows
        if (version_id, playbook) not in excluded.get(bot_id, frozenset())
    ]
    return build_gate_evidence(outcomes, discounted_benchmark(weighted / count, discount))
