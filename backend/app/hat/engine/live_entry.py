"""The live trigger service: candidates from the database in, signals out (HAT-BOT-052).

`TriggerMonitor` (`trigger.py`) is pure; this module is what it needs around it:

- `watchlist_candidates` reads the day's Watchlisted rows of the active bots and turns each into a
  `Candidate` (setup levels from the row, the playbook from the strategy version the row was
  scanned under, the last configured max-chase percentage from the bot's versions, and the bot's
  effective mode for the session, which decides where `catalyst_attached` blocks);
- `ep_candidates` builds the candidates of the pre-open playbooks (`Playbook.preopen`, such as
  the episodic pivot) from the 09:16 gap scan: only gap-scan candidates (10%/20% band or F&O,
  gapped at least 10%, in the universe) whose setup passes on the daily bars up to the previous
  session;
- `submit_trigger` turns a fired trigger into a signal through a `Route`: the execution router
  (`execution/router.py`), or by default `entries.open_entry` under the bot's effective mode, so
  every entry still passes `evaluate_signal` under the bot's and the owner's lock (a skipped
  stop or entry filter is stored as a Rejected signal with its reason);
- `TriggerService` attaches the monitor to `LiveData` as a bar listener and hands each trigger to
  a worker thread (the database work never runs on the loop);
- `DataHealthKill` is the `EventFeedHealth` hook that trips the account's data-health kill while
  the feed is stale and clears it when ticks resume (HAT-DATA-116, HAT-RISK-063).

Every active bot is watched, whatever its mode. A bot's effective mode is the one in force at the
session open when its sleeve is Live and every gate of its account passes
(`execution/modes.py:bot_mode`, `gate_refusal`: GTT delivery, DDPI confirmed, weights
confirmed), else shadow. The Fyers adapter reports GTT delivery, so a confirm or auto bot that
passes them places real orders. An auto entry needs the router to
place its order and a confirm entry needs it to sign its card: the default route refuses both
(`auto_needs_router`, `confirm_needs_router`) rather than leave one unplaced or unanswerable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..data.bars import Bar
from ..data.demerger_notice import SKIP_DEMERGER_PENDING, entry_blocked
from ..data.gapscan import Gapper, is_candidate
from ..execution.modes import bot_mode
from ..governance import Mode
from ..models import HatBot, HatInstrument, HatStrategyVersion, HatWatchlistEntry
from ..money import exact
from ..risk import kill_ops
from ..risk.decision import SignalContext
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar
from ..time import IST, utc_now
from . import entries, lifecycle, positions
from .criteria import CriterionResult, EvalContext
from .rescale import SKIP_ACTION_PENDING, awaits_rescale
from .strategy.config import Playbook, StopRule, StrategyConfig, parse_config
from .strategy.scoring import evaluate_stage
from .trigger import (
    Candidate,
    DailyInputs,
    Trigger,
    TriggerMonitor,
    daily_trigger_results,
    last_configured_chase,
    watchlist_levels,
)

__all__ = [
    "ContextSource",
    "DataHealthKill",
    "Route",
    "TriggerService",
    "ep_candidates",
    "ep_setup_passes",
    "route_by_mode",
    "submit_trigger",
    "watchlist_candidates",
]

logger = logging.getLogger(__name__)

ACTIVE = "active"
FEED_HEALTH = "feed_health"  # the feed's own health reason: its recovery never clears the gate
_INSTANT = timedelta(microseconds=1)

# one instrument's `DailyInputs`, with the stop history of the candidate's own rule
DailySource = Callable[[uuid.UUID, StopRule], DailyInputs]
BarsSource = Callable[[uuid.UUID], pl.DataFrame]  # adjusted daily bars of one instrument


def _daily_results(
    playbook: Playbook, bars: BarsSource | None, instrument_id: uuid.UUID, trade_date: date
) -> dict[int, CriterionResult]:
    """The daily-bar trigger results of one candidate; none without a bars source, so such a
    trigger fails closed."""
    if bars is None:
        return {}
    return daily_trigger_results(playbook, bars(instrument_id), trade_date)


def _active_bots(session: Session) -> list[HatBot]:
    return list(
        session.scalars(
            select(HatBot).where(HatBot.status == ACTIVE, HatBot.live_version_id.is_not(None))
        )
    )


def _session_mode(
    session: Session, bot: HatBot, trade_date: date, calendar: SessionCalendar | None
) -> str:
    """The bot's effective mode for `trade_date`'s session, read at its open (09:15 IST when the
    calendar does not give one)."""
    day = calendar.days.get(trade_date) if calendar is not None else None
    opens = day.open_at if day is not None and day.open_at is not None else None
    at = opens or datetime.combine(trade_date, time(9, 15), tzinfo=IST)
    return bot_mode(session, bot, at, calendar)


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
    session: Session,
    trade_date: date,
    daily: DailySource,
    bars: BarsSource | None = None,
    *,
    calendar: SessionCalendar | None = None,
) -> list[Candidate]:
    """One candidate per Watchlisted row of `trade_date` of an active bot, judged by the playbook
    of the strategy version the row was scanned under, under the bot's effective mode for the
    session. `bars` feeds the playbook's daily-bar trigger criteria."""
    out: list[Candidate] = []
    for bot in _active_bots(session):
        mode = _session_mode(session, bot, trade_date, calendar)
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
            out.append(
                Candidate(
                    bot_id=bot.id,
                    strategy_key=config.bot,  # the key of the risk defaults (the seed names)
                    mode=mode,
                    playbook=playbook,
                    instrument_id=row.instrument_id,
                    symbol=symbols[row.instrument_id],
                    trade_date=trade_date,
                    daily=daily(row.instrument_id, playbook.stop),
                    levels=watchlist_levels(row.pivot, levels),
                    final_contraction_low=exact(
                        levels.get("final_contraction_low"), floats=True, positive=True
                    ),
                    configured_max_chase_pct=last_configured_chase(configs, playbook.name),
                    daily_results=_daily_results(playbook, bars, row.instrument_id, trade_date),
                )
            )
    return out


def _playbook(config: StrategyConfig, name: str) -> Playbook | None:
    return next((p for p in config.playbooks if p.name == name), None)


def ep_setup_passes(playbook: Playbook, bars: pl.DataFrame, trade_date: date) -> bool:
    """A pre-open playbook's setup on the daily bars up to the previous session: every enabled hard
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
    # Threshold 0: only the hard criteria decide; the stage passes earlier levels on (HAT-CRIT-123).
    return evaluate_stage("setup", playbook.setup.criteria, ctx, 0.0).passed


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
    *,
    calendar: SessionCalendar | None = None,
) -> list[Candidate]:
    """The candidates of every pre-open playbook (`Playbook.preopen`) of each active bot's live
    version, for the instruments the gap scan selected (already filtered by `ep_candidates`),
    under the bot's effective mode for the session. `bars` feeds the playbook's daily-bar
    trigger criteria."""
    out: list[Candidate] = []
    for bot in _active_bots(session):
        version = session.get(HatStrategyVersion, bot.live_version_id)
        if version is None:
            continue
        config = parse_config(version.config)
        preopen = [p for p in config.playbooks if p.preopen]
        if not preopen:
            continue
        configs = _versions_newest_first(session, bot.strategy_id)
        mode = _session_mode(session, bot, trade_date, calendar)
        out.extend(
            Candidate(
                bot_id=bot.id,
                strategy_key=config.bot,  # the key of the risk defaults (the seed names)
                mode=mode,
                playbook=playbook,
                instrument_id=item.instrument_id,
                symbol=item.symbol,
                trade_date=trade_date,
                daily=daily(item.instrument_id, playbook.stop),
                open_price=item.open_price,
                announcements=item.announcements,
                configured_max_chase_pct=last_configured_chase(configs, playbook.name),
                daily_results=_daily_results(playbook, bars, item.instrument_id, trade_date),
            )
            for playbook in preopen
            for item in instruments
        )
    return out


# --- from trigger to signal -------------------------------------------------------------------


def entry_request(
    trigger: Trigger, base: SignalContext, *, feed_stale: bool | None = None
) -> entries.EntryRequest:
    """The `EntryRequest` of a fired trigger: the market-side context `base` with the trigger's
    prices, stop, level and circuit state, and the skip reason when its stop or a filter failed.
    `feed_stale`, when given, is the live feed state at the trigger and replaces `base`'s."""
    candidate, plan = trigger.candidate, trigger.plan
    context = replace(
        base,
        strategy_key=candidate.strategy_key,
        entry_price=plan.entry,
        stop_price=plan.stop,
        trigger_level=plan.level,
        locked_upper_circuit=plan.locked_upper_circuit,
        configured_max_chase_pct=candidate.configured_max_chase_pct,
        adv20=candidate.daily.adv20,
        feed_stale=base.feed_stale if feed_stale is None else feed_stale,
    )
    criteria = tuple(
        {"stage": stage, **outcome.to_json()}
        for stage, outcomes in (
            ("trigger", trigger.evaluation.outcomes),
            ("entry_filter", plan.filters),
        )
        for outcome in outcomes
    )
    return entries.EntryRequest(
        bot_id=candidate.bot_id,
        playbook=candidate.playbook.name,
        instrument_id=candidate.instrument_id,
        trade_date=candidate.trade_date,
        triggered_at=trigger.at,
        context=context,
        skip_reason=plan.skip,
        criteria=criteria,
    )


class Route(Protocol):
    """Where a trigger's `EntryRequest` goes: `route_by_mode`, or `daily.entries.LiveRoute`, which
    reads a fresh `PlacementCheck` and calls `ExecutionRouter.route` (that method is not a `Route`:
    it also takes `now` and `check`)."""

    def __call__(
        self,
        session: Session,
        request: entries.EntryRequest,
        *,
        decide: entries.DECIDE = evaluate_signal,
    ) -> entries.EntryOutcome: ...


def route_by_mode(
    session: Session,
    request: entries.EntryRequest,
    *,
    decide: entries.DECIDE = evaluate_signal,
) -> entries.EntryOutcome:
    """The route without a broker: `open_entry` under the bot's effective mode, in the caller's
    transaction (no commit). An auto bot needs the execution router to place its order, and a
    confirm bot needs it to sign its approval card's token (PR 16), so each is refused here
    (`auto_needs_router`, `confirm_needs_router`) and nothing is written: only shadow is routed."""
    bot = session.get(HatBot, request.bot_id)
    if bot is None:
        raise positions.TradeError("unknown_bot")
    mode = bot_mode(session, bot, request.triggered_at, request.context.calendar)
    if mode == Mode.AUTO:
        raise positions.TradeError("auto_needs_router")
    if mode == Mode.CONFIRM:
        raise positions.TradeError("confirm_needs_router")
    return entries.open_entry(session, request, decide=decide, mode=mode)


def submit_trigger(
    session: Session,
    trigger: Trigger,
    base: SignalContext,
    *,
    feed_stale: bool | None = None,
    decide: entries.DECIDE = evaluate_signal,
    route: Route = route_by_mode,
) -> entries.EntryOutcome:
    """Write the signal of a fired trigger through `route` (the default writes in the caller's
    transaction and does not commit; the execution router commits its own steps). An instrument
    whose lineage chain holds an open position awaiting a split or bonus rescale, or under
    another corporate-action lock (an unhandled action, a split-like gap, an ISIN change), is
    manage-only: its trigger is stored skipped, `corporate_action_pending` (HAT-EXEC-142, Q4).
    One with a demerger notice is skipped `demerger_pending` from the session before its ex-date
    until its factor is stored (`demerger_notice.entry_blocked`, Q5)."""
    request = entry_request(trigger, base, feed_stale=feed_stale)
    if awaits_rescale(session, request.instrument_id, request.trade_date):
        request = replace(request, skip_reason=SKIP_ACTION_PENDING)
    elif _demerger_pending(session, request):
        request = replace(request, skip_reason=SKIP_DEMERGER_PENDING)
    return route(session, request, decide=decide)


def _demerger_pending(session: Session, request: entries.EntryRequest) -> bool:
    calendar = request.context.calendar
    sessions = calendar.trading_days() if isinstance(calendar, SessionCalendar) else []
    return entry_blocked(session, request.instrument_id, request.trade_date, sessions)


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
        route: Route = route_by_mode,
    ) -> None:
        self.monitor = monitor
        self._session_factory = session_factory
        self._contexts = contexts
        self._feed_stale = feed_stale
        self._route = route

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

    def write(self, trigger: Trigger, stale: bool) -> entries.EntryOutcome | None:
        """Write one trigger's signal and commit, then tell the monitor: a stored signal (a new
        one or the existing one) closes the key for the day; a failure is logged, leaves
        nothing and releases the key, so the next final bar evaluates it again."""
        key = trigger.candidate.key
        try:
            with self._session_factory() as session:
                base = self._contexts.entry_context(session, trigger.candidate.bot_id, trigger.at)
                outcome = submit_trigger(
                    session, trigger, base, feed_stale=stale, route=self._route
                )
                session.commit()
        except Exception:
            logger.exception("writing a trigger failed")
            self.monitor.release(key)
            return None
        self.monitor.confirm(key)
        return outcome


class DataHealthKill:
    """`EventFeedHealth`'s `on_change`: a stale feed trips the account's `feed_health` kill (every
    bot manage-only, exits untouched); recovery clears that reason by itself (HAT-RISK-063), never
    the quality gate's `data_health` trip (HAT-DATA-065). The write
    runs on a worker thread when called from the loop. Changes are numbered when they happen
    and written one at a time; a change that reaches the database after a newer one is dropped,
    so the kill always ends in the state of the latest change, whatever order the workers run."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        clock: Clock = SYSTEM_CLOCK,
        submit: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._submit = submit or TriggerService._submit
        self._last: datetime | None = None
        self._seq = 0
        self._written = 0
        self._lock = threading.Lock()

    def __call__(self, stale: bool) -> None:
        # Each change gets an instant after the previous one: a trip and a recovery at the same
        # instant would read as tripped (a tie favours the trip), leaving the kill on.
        at = utc_now(self._clock)
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
                kill_ops.sync_health(
                    session,
                    scope="account",
                    bot_id=None,
                    failing=stale,
                    reason=FEED_HEALTH,
                    now=at,
                )
                session.commit()
        except Exception:
            logger.exception("data health kill update failed")
