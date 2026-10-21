"""The live trigger service: candidates from the database in, signals out (07 §12, HAT-BOT-052).

`TriggerMonitor` (`trigger.py`) is pure; this module is what it needs around it:

- `watchlist_candidates` reads the day's Watchlisted rows of the shadow bots and turns each into a
  `Candidate` (setup levels from the row, the playbook from the strategy version the row was
  scanned under, the last configured max-chase percentage from the bot's versions);
- `ep_candidates` builds the episodic-pivot candidates from the 09:16 gap scan: only gap-scan
  candidates (10%/20% band or F&O, gapped at least 10%, in the universe) whose EP setup passes
  on the daily bars up to the previous session;
- `submit_trigger` turns a fired trigger into a signal through `trades.open_entry`, so every
  entry still passes `evaluate_signal` under the bot's lock (a skipped stop or entry filter is
  stored as a Rejected signal with its reason);
- `TriggerService` attaches the monitor to `LiveData` as a bar listener and hands each trigger to
  a worker thread (the database work never runs on the loop);
- `DataHealthKill` is the `EventFeedHealth` hook that trips the account's data-health kill while
  the feed is stale and clears it when ticks resume (HAT-DATA-116, HAT-RISK-063).

Only shadow bots are watched here. Confirm and auto go through the execution router, which arrives
with PR 15b; until then `open_entry` refuses them and no candidate is built for them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.bars import Bar
from ..data.gapscan import Gapper, is_candidate
from ..models import HatBot, HatInstrument, HatStrategyVersion, HatWatchlistEntry
from ..risk import controls
from ..risk.decision import SignalContext
from ..time import utc_now
from . import lifecycle, trades
from .criteria import CriterionResult, EvalContext, evaluate
from .criteria.live import CONSOLIDATION_HIGH, LEVEL_PIVOT
from .scan import EP_PLAYBOOK
from .strategy.config import Playbook, StrategyConfig, parse_config
from .trigger import (
    Candidate,
    DailyInputs,
    Trigger,
    TriggerMonitor,
    daily_trigger_results,
    last_configured_chase,
)

__all__ = [
    "ContextSource",
    "DataHealthKill",
    "TriggerService",
    "ep_candidates",
    "ep_setup_passes",
    "submit_trigger",
    "watchlist_candidates",
]

logger = logging.getLogger(__name__)

SHADOW = "shadow"
ACTIVE = "active"
DATA_HEALTH = "data_health"
_INSTANT = timedelta(microseconds=1)

DailySource = Callable[[uuid.UUID], DailyInputs]
BarsSource = Callable[[uuid.UUID], pl.DataFrame]  # adjusted daily bars of one instrument


def _daily_results(
    playbook: Playbook, bars: BarsSource | None, instrument_id: uuid.UUID, trade_date: date
) -> dict[int, CriterionResult]:
    """The daily-bar trigger results of one candidate; none without a bars source, so such a
    trigger fails closed."""
    if bars is None:
        return {}
    return daily_trigger_results(playbook, bars(instrument_id), trade_date)


def _decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _shadow_bots(session: Session) -> list[HatBot]:
    return list(
        session.scalars(
            select(HatBot).where(
                HatBot.status == ACTIVE,
                HatBot.execution_mode == SHADOW,
                HatBot.live_version_id.is_not(None),
            )
        )
    )


def _versions_newest_first(session: Session, strategy_id: uuid.UUID) -> list[StrategyConfig]:
    rows = session.scalars(
        select(HatStrategyVersion)
        .where(HatStrategyVersion.strategy_id == strategy_id)
        .order_by(HatStrategyVersion.version.desc())
    )
    return [parse_config(row.config) for row in rows]


def _symbols(session: Session, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = set(ids)
    if not wanted:
        return {}
    return dict(
        session.execute(
            select(HatInstrument.id, HatInstrument.symbol).where(HatInstrument.id.in_(wanted))
        ).all()
    )


def watchlist_candidates(
    session: Session, trade_date: date, daily: DailySource, bars: BarsSource | None = None
) -> list[Candidate]:
    """One candidate per Watchlisted row of `trade_date` of an active shadow bot, judged by the
    playbook of the strategy version the row was scanned under. `bars` feeds the playbook's
    daily-bar trigger criteria."""
    out: list[Candidate] = []
    for bot in _shadow_bots(session):
        configs = _versions_newest_first(session, bot.strategy_id)
        rows = list(
            session.scalars(
                select(HatWatchlistEntry).where(
                    HatWatchlistEntry.bot_id == bot.id,
                    HatWatchlistEntry.trade_date == trade_date,
                    HatWatchlistEntry.status == lifecycle.WATCHLISTED,
                )
            )
        )
        symbols = _symbols(session, (r.instrument_id for r in rows))
        for row in rows:
            version = session.get(HatStrategyVersion, row.strategy_version_id)
            if version is None or row.instrument_id not in symbols:
                continue
            config = parse_config(version.config)
            playbook = _playbook(config, row.playbook)
            if playbook is None:
                continue
            levels = (row.criteria_results or {}).get("levels", {})
            pivot = _decimal(row.pivot)
            out.append(
                Candidate(
                    bot_id=bot.id,
                    strategy_key=config.bot,  # the key of the risk defaults (the seed names)
                    mode=SHADOW,
                    playbook=playbook,
                    instrument_id=row.instrument_id,
                    symbol=symbols[row.instrument_id],
                    trade_date=trade_date,
                    daily=daily(row.instrument_id),
                    levels={
                        LEVEL_PIVOT: pivot,
                        CONSOLIDATION_HIGH: _decimal(levels.get(CONSOLIDATION_HIGH)),
                    },
                    final_contraction_low=_decimal(levels.get("final_contraction_low")),
                    configured_max_chase_pct=last_configured_chase(configs, playbook.name),
                    daily_results=_daily_results(playbook, bars, row.instrument_id, trade_date),
                )
            )
    return out


def _playbook(config: StrategyConfig, name: str) -> Playbook | None:
    return next((p for p in config.playbooks if p.name == name), None)


def ep_setup_passes(playbook: Playbook, bars: pl.DataFrame, trade_date: date) -> bool:
    """The EP playbook's setup on the daily bars up to the previous session: every enabled hard
    setup criterion passes (`neglect` reads the bars before `trade_date` itself). No bars, or a
    setup with no enabled criterion, does not pass (fail closed)."""
    enabled = [c for c in playbook.setup.criteria if c.enabled and c.mode == "hard"]
    if not enabled or bars.is_empty():
        return False
    # The trade-date bar does not exist at the open. The EP setup criteria read the bars before
    # the evaluation date (`previous_history`); one that needs the day's own bar fails closed.
    ctx = EvalContext(
        as_of=trade_date, instrument_id="", bars=bars.filter(pl.col("date") < trade_date)
    )
    return all(evaluate(c.type, c.params, ctx).passed for c in enabled)


def ep_candidates(gappers: Sequence[Gapper], setup_passes: Callable[[str], bool]) -> list[str]:
    """The EP candidates of a gap scan (HAT-BOT-052): the scan's candidates (gap at least 10%,
    universe passed, 10%/20% band or F&O) whose EP setup passes, in scan order."""
    return [g.row.symbol for g in gappers if is_candidate(g) and setup_passes(g.row.symbol)]


@dataclass(frozen=True)
class EpInputs:
    """One EP candidate instrument: its id, symbol, open price and announcement instants."""

    instrument_id: uuid.UUID
    symbol: str
    open_price: Decimal | None
    announcements: tuple[datetime, ...] = ()


def ep_bot_candidates(
    session: Session,
    trade_date: date,
    instruments: Sequence[EpInputs],
    daily: DailySource,
    bars: BarsSource | None = None,
) -> list[Candidate]:
    """The EP candidates of every active shadow bot with an `episodic_pivot` playbook, for the
    instruments the gap scan selected (already filtered by `ep_candidates`). `bars` feeds the
    playbook's daily-bar trigger criteria."""
    out: list[Candidate] = []
    for bot in _shadow_bots(session):
        version = session.get(HatStrategyVersion, bot.live_version_id)
        if version is None:
            continue
        config = parse_config(version.config)
        playbook = _playbook(config, EP_PLAYBOOK)
        if playbook is None:
            continue
        configs = _versions_newest_first(session, bot.strategy_id)
        out.extend(
            Candidate(
                bot_id=bot.id,
                strategy_key=config.bot,  # the key of the risk defaults (the seed names)
                mode=SHADOW,
                playbook=playbook,
                instrument_id=item.instrument_id,
                symbol=item.symbol,
                trade_date=trade_date,
                daily=daily(item.instrument_id),
                open_price=item.open_price,
                announcements=item.announcements,
                configured_max_chase_pct=last_configured_chase(configs, playbook.name),
                daily_results=_daily_results(playbook, bars, item.instrument_id, trade_date),
            )
            for item in instruments
        )
    return out


# --- from trigger to signal -------------------------------------------------------------------


def entry_request(trigger: Trigger, base: SignalContext) -> trades.EntryRequest:
    """The `EntryRequest` of a fired trigger: the market-side context `base` with the trigger's
    prices, stop, level and circuit state, and the skip reason when its stop or a filter failed."""
    candidate, plan = trigger.candidate, trigger.plan
    context = replace(
        base,
        strategy_key=candidate.strategy_key,
        entry_price=plan.entry,
        stop_price=plan.stop,
        trigger_level=plan.level,
        ltp=plan.entry,
        locked_upper_circuit=plan.locked_upper_circuit,
        configured_max_chase_pct=candidate.configured_max_chase_pct,
        adv20=candidate.daily.adv20,
    )
    criteria = tuple(
        {"stage": stage, **outcome.to_json()}
        for stage, outcomes in (
            ("trigger", trigger.evaluation.outcomes),
            ("entry_filter", plan.filters),
        )
        for outcome in outcomes
    )
    return trades.EntryRequest(
        bot_id=candidate.bot_id,
        playbook=candidate.playbook.name,
        instrument_id=candidate.instrument_id,
        trade_date=candidate.trade_date,
        triggered_at=trigger.at,
        context=context,
        skip_reason=plan.skip,
        criteria=criteria,
    )


def submit_trigger(
    session: Session,
    trigger: Trigger,
    base: SignalContext,
    *,
    decide: trades.DECIDE = trades.evaluate_signal,
) -> trades.EntryOutcome:
    """Write the signal of a fired trigger in the caller's transaction (no commit)."""
    return trades.open_entry(session, entry_request(trigger, base), decide=decide)


class ContextSource(Protocol):
    """The market-side inputs of a bot's entry at `at` (equity, allocation, cash, calendar, gate,
    quality, kill, ladder and policy versions). The daily-cycle wiring supplies it (PR 17)."""

    def entry_context(self, session: Session, bot_id: uuid.UUID, at: datetime) -> SignalContext: ...


class TriggerService:
    """The monitor as a `LiveData` bar listener. `on_bar` runs on the loop thread and only
    evaluates; each trigger is written by `write` on a worker thread in its own transaction.
    `feed_stale` is read at the trigger: the entry's `feed_stale` input is the live state, never
    the context source's, so a stale feed rejects the entry (invariant 4)."""

    def __init__(
        self,
        monitor: TriggerMonitor,
        session_factory: Callable[[], Session],
        contexts: ContextSource,
        feed_stale: Callable[[], bool],
    ) -> None:
        self.monitor = monitor
        self._session_factory = session_factory
        self._contexts = contexts
        self._feed_stale = feed_stale

    def on_bar(self, bar: Bar) -> list[Trigger]:
        fired = self.monitor.on_bar(bar)
        stale = self._feed_stale()
        for trigger in fired:
            self._submit(lambda t=trigger: self.write(t, stale))
        return fired

    @staticmethod
    def _submit(work: Callable[[], None]) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, work)
        except RuntimeError:  # no loop (a plain thread or a check): write inline
            work()

    def write(self, trigger: Trigger, stale: bool) -> trades.EntryOutcome | None:
        """Write one trigger's signal and commit. A failure is logged and leaves nothing."""
        try:
            with self._session_factory() as session:
                base = self._contexts.entry_context(session, trigger.candidate.bot_id, trigger.at)
                outcome = submit_trigger(session, trigger, replace(base, feed_stale=stale))
                session.commit()
                return outcome
        except Exception:
            logger.exception("writing a trigger failed")
            return None


class DataHealthKill:
    """`EventFeedHealth`'s `on_change`: a stale feed trips the account's data-health kill (every
    bot manage-only, exits untouched); recovery clears it by itself (HAT-RISK-063). The write
    runs on a worker thread when called from the loop. Changes are numbered when they happen
    and written one at a time; a change that reaches the database after a newer one is dropped,
    so the kill always ends in the state of the latest change, whatever order the workers run."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        now: Callable[[], datetime] = utc_now,
        submit: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._submit = submit or TriggerService._submit
        self._last: datetime | None = None
        self._seq = 0
        self._written = 0
        self._lock = threading.Lock()

    def __call__(self, stale: bool) -> None:
        # Each change gets an instant after the previous one: a trip and a recovery at the same
        # instant would read as tripped (a tie favours the trip), leaving the kill on.
        at = self._now()
        if self._last is not None and at <= self._last:
            at = self._last + _INSTANT
        self._last = at
        self._seq += 1
        seq = self._seq
        self._submit(lambda: self.write(stale, at, seq))

    def write(self, stale: bool, at: datetime, seq: int) -> None:
        with self._lock:
            if seq <= self._written:
                return  # a newer change is already written
            self._written = seq
            self._write(stale, at)

    def _write(self, stale: bool, at: datetime) -> None:
        try:
            with self._session_factory() as session:
                controls.sync_health(
                    session,
                    scope="account",
                    bot_id=None,
                    failing=stale,
                    reason=DATA_HEALTH,
                    now=at,
                )
                session.commit()
        except Exception:
            logger.exception("data health kill update failed")
