"""The live trigger service: candidates from the database in, signals out (HAT-BOT-052).

`TriggerMonitor` (`trigger.py`) is pure; this module is what it needs around it:

- `watchlist_candidates` reads the day's Watchlisted rows of the active bots and turns each into a
  `Candidate` (setup levels from the row, the playbook from the strategy version the row was
  scanned under, the last configured max-chase percentage from the bot's versions, and the bot's
  effective mode for the session, which decides where `catalyst_attached` blocks);
- `ep_candidates` builds the candidates of the pre-open playbooks (`Playbook.preopen`, such as
  the episodic pivot) from the 09:16 gap scan: only gap-scan candidates (gapped at least the
  active bots' least `gap_up` minimum, `gap_floor`, with a band that allows it or F&O, in the
  universe) whose setup passes on the daily bars up to the previous session;
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
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..configs.records import Bot, StrategyVersion
from ..configs.unit import configs_of
from ..data.bars import Bar
from ..data.demerger_notice import SKIP_DEMERGER_PENDING, entry_blocked
from ..data.gapscan import Gapper, is_candidate
from ..data.results.derive import growth_by_result, results_as_of
from ..data.sessions import SESSION_OPEN
from ..execution.modes import bot_mode
from ..governance import Mode
from ..models import HatInstrument, HatWatchlistEntry
from ..money import exact
from ..notify import Notifier
from ..risk import kill_ops
from ..risk.decision import SignalContext
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar
from ..time import IST, as_utc, trade_date_of, utc_now
from . import entries, lifecycle, positions
from .criteria import CONTRACTION_LOW, CriterionResult, EvalContext, evaluate, get_type
from .criteria.live import ResultEvent
from .criteria.setup import RANGE_SESSIONS, NeglectParams, neglect, range_pct
from .criteria.universe import AdrMinParams, adr_min
from .rescale import SKIP_ACTION_PENDING, awaits_rescale
from .strategy.config import Instance, Playbook, StopRule, StrategyConfig, parse_config
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
    "active_gap_floor",
    "ep_candidates",
    "ep_gate",
    "ep_stock_log",
    "ep_results",
    "reads_results",
    "result_event",
    "session_open",
    "ep_setup_passes",
    "gap_floor",
    "route_by_mode",
    "submit_trigger",
    "watchlist_candidates",
]

logger = logging.getLogger(__name__)

ACTIVE = "active"
FEED_HEALTH = "feed_health"  # the feed's own health reason: its recovery never clears the gate
_INSTANT = timedelta(microseconds=1)
ALERT_FEED_HEALTH_UNWRITTEN = "feed_health.unwritten"
# Their own threads, apart from the loop's default pool (the Telegram long poll, the relay, the
# scheduler's reads): a trigger write routes a real auto entry, so a slow database step
# elsewhere never queues it (audit A3 F6). The feed-health kill writes one at a time.
TRIGGER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hat-trigger")
HEALTH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hat-health")

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


def _active_bots(session: Session) -> list[Bot]:
    return configs_of(session).all(
        Bot, lambda b: b.status == ACTIVE and b.live_version_id is not None
    )


def _session_mode(
    session: Session, bot: Bot, trade_date: date, calendar: SessionCalendar | None
) -> str:
    """The bot's effective mode for `trade_date`'s session, read at its open (09:15 IST when the
    calendar does not give one)."""
    day = calendar.days.get(trade_date) if calendar is not None else None
    opens = day.open_at if day is not None and day.open_at is not None else None
    at = opens or datetime.combine(trade_date, time(9, 15), tzinfo=IST)
    return bot_mode(session, bot, at, calendar)


def _versions_newest_first(session: Session, strategy_id: uuid.UUID) -> list[StrategyConfig]:
    rows = configs_of(session).all(StrategyVersion, lambda v: v.strategy_id == strategy_id)
    return [parse_config(row.config) for row in sorted(rows, key=lambda v: -v.version)]


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
            version = configs_of(session).get(StrategyVersion, row.strategy_version_id)
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
                    levels=watchlist_levels(levels),
                    final_contraction_low=exact(
                        levels.get(CONTRACTION_LOW), floats=True, positive=True
                    ),
                    configured_max_chase_pct=last_configured_chase(configs, playbook.name),
                    daily_results=_daily_results(playbook, bars, row.instrument_id, trade_date),
                )
            )
    return out


def _playbook(config: StrategyConfig, name: str) -> Playbook | None:
    return next((p for p in config.playbooks if p.name == name), None)


def ep_setup_passes(
    playbook: Playbook,
    bars: pl.DataFrame,
    trade_date: date,
    previous: date | None,
    universe: Sequence[Instance] = (),
    series: str | None = None,
) -> bool:
    """A pre-open playbook's universe and setup on the daily bars up to `previous`, the market's
    last session before `trade_date`: every enabled `universe` criterion (the playbook's
    effective universe stage, `StrategyConfig.universe_for`, judged with the instrument's
    `series`) and every enabled hard setup criterion passes. A universe or setup-stage type
    (`adr_min`, `sma_above`, ...) is evaluated as of `previous`, as the nightly scan would that
    evening; a trigger-stage type over daily bars (`neglect`) as of `trade_date`, reading the
    bars before it. Either way the trade date's own bar, which does not exist at the open, is
    never read. Like the nightly scan, which evaluates only the members with a bar on its date,
    an instrument without a bar on `previous` does not pass, nor do no bars (fail closed). A
    setup whose hard criteria are all switched off passes: the universe and the trigger still
    decide, as a nightly setup with no enabled criterion does."""
    return ep_gate(playbook, bars, trade_date, previous, universe, series) is None


EP_UNIVERSE = "universe"
ADR_DAYS = 20
_PLACES = 4
EP_SETUP = "setup"


def ep_gate(
    playbook: Playbook,
    bars: pl.DataFrame,
    trade_date: date,
    previous: date | None,
    universe: Sequence[Instance] = (),
    series: str | None = None,
) -> str | None:
    """`ep_setup_passes`'s verdict as the gate that failed: `universe`, `setup` or the type of
    the first failing hard setup criterion (`neglect`), None when all pass. The universe stage
    reads the bars and the series only, as the nightly scan's does (no price bands: no stored
    config's universe uses `price_band_allowed`)."""
    enabled = [c for c in playbook.setup.criteria if c.enabled and c.mode == "hard"]
    before = bars.filter(pl.col("date") < trade_date)
    if before.is_empty() or previous is None or before["date"].max() != previous:
        return EP_SETUP
    on_gap_day = EvalContext(as_of=trade_date, instrument_id="", bars=before)
    previous_ctx = EvalContext(as_of=previous, instrument_id="", bars=before, series=series)
    for instance in sorted((u for u in universe if u.enabled), key=lambda i: i.order):
        if not evaluate(instance.type, instance.params, previous_ctx).passed:
            return EP_UNIVERSE
    for instance in sorted(enabled, key=lambda i: i.order):
        ctx = on_gap_day if get_type(instance.type).stage == "trigger" else previous_ctx
        if not evaluate(instance.type, instance.params, ctx).passed:
            return instance.type
    return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, _PLACES)


def ep_stock_log(bars: pl.DataFrame, previous: date) -> dict[str, float | None]:
    """A pre-open candidate's logged stock values from its daily bars through `previous` (the
    backtest's EP log): the 126-session range (`range_pct`), the ADR(20) (`adr_min`'s value)
    and the 63-session return (`neglect`'s), None where the bars are short."""
    ctx = EvalContext(as_of=previous, instrument_id="", bars=bars)
    history = ctx.history
    span = range_pct(history.high[-RANGE_SESSIONS:], history.low[-RANGE_SESSIONS:])
    if len(history) < RANGE_SESSIONS:
        span = None
    adr = adr_min(ctx, AdrMinParams(days=ADR_DAYS, pct=0.0)).value
    # neglect reads the sessions before its date: judged as of the day after `previous`
    before = bars.filter(pl.col("date") <= previous)
    after = EvalContext(as_of=previous + timedelta(days=1), instrument_id="", bars=before)
    ret = neglect(after, NeglectParams(max_return_63d_pct=100_000)).value
    return {
        "range_126d_pct": _round(span),
        "adr20_pre_gap_pct": _round(adr),
        "ret_63d_pct": _round(ret),
    }


def ep_candidates(
    gappers: Sequence[Gapper], setup_passes: Callable[[str], bool], minimum_pct: Decimal
) -> list[str]:
    """The EP candidates of a gap scan (HAT-BOT-052): the scan's candidates (gap at least
    `minimum_pct`, universe passed, a band of at least `minimum_pct` or F&O) whose EP setup
    passes, in scan order."""
    return [
        g.row.symbol for g in gappers if is_candidate(g, minimum_pct) and setup_passes(g.row.symbol)
    ]


def gap_floor(configs: Iterable[StrategyConfig]) -> Decimal | None:
    """The gap scan's floor: the least enabled `gap_up` `min_pct` of the pre-open playbooks of
    `configs` (`Playbook.gap_min_pct`), None when none has one (the scan has nothing to look
    for). Each playbook's own `gap_up` still applies its own minimum at the trigger."""
    found = [
        pct
        for config in configs
        for p in config.playbooks
        if p.preopen and (pct := p.gap_min_pct) is not None
    ]
    return min(found, default=None)


def active_gap_floor(session: Session) -> Decimal | None:
    """`gap_floor` of the active bots' live versions."""
    configs = []
    for bot in _active_bots(session):
        version = configs_of(session).get(StrategyVersion, bot.live_version_id)
        if version is not None:
            configs.append(parse_config(version.config))
    return gap_floor(configs)


@dataclass(frozen=True)
class EpInputs:
    """One EP candidate instrument: its id, symbol, open price, announcement instants, its
    series (the universe stage reads it) and its results filings public after `results_since`
    (`ep_results`)."""

    instrument_id: uuid.UUID
    symbol: str
    open_price: Decimal | None
    announcements: tuple[datetime, ...] = ()
    series: str | None = None
    results: tuple[ResultEvent, ...] = ()
    results_since: datetime | None = None


def session_open(day: date) -> datetime:
    """09:15 IST of `day`: where `earnings_catalyst`'s window opens on the previous session."""
    return datetime.combine(day, SESSION_OPEN, tzinfo=IST)


def ep_results(
    session: Session, instrument_id: uuid.UUID, since: datetime, at: datetime
) -> tuple[ResultEvent, ...]:
    """The instrument's results filings known at `at` (`results_as_of`) public after `since`,
    each with the growth of its quarter as known when it became public."""
    rows = [r for r in results_as_of(session, instrument_id, at) if as_utc(r.public_at) > since]
    growth = growth_by_result(session, [r.id for r in rows])
    return tuple(result_event(r.public_at, r.period_end, growth.get(r.id)) for r in rows)


def reads_results(playbook: Playbook) -> bool:
    """Whether `playbook` enables `earnings_catalyst` (its candidates read results filings)."""
    return any(i.enabled and i.type == "earnings_catalyst" for i in playbook.trigger)


def result_event(public_at: datetime, period_end: date, growth: object) -> ResultEvent:
    """A `ResultEvent` from a result's instant and quarter and its growth row (None: no growth
    known, so the growth rule fails closed)."""
    return ResultEvent(
        as_utc(public_at),
        period_end,
        getattr(growth, "sales_yoy", None),
        getattr(growth, "eps_yoy", None),
        getattr(growth, "turnaround", None),
    )


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
        version = configs_of(session).get(StrategyVersion, bot.live_version_id)
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
                results=item.results,
                results_since=item.results_since,
                universe=tuple(config.universe_for(playbook.name)),
                series=item.series,
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
    bot = configs_of(session).get(Bot, request.bot_id)
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
    def _submit(work: Callable[[], None], pool: Executor = TRIGGER_POOL) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(pool, work)
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
    runs on its own worker thread when called from the loop. Changes are numbered when they happen
    and written one at a time; a change that reaches the database after a newer one is dropped,
    so the kill always ends in the state of the latest change, whatever order the workers run.
    A failed write leaves its change pending (audit A2 F6): `retry` (housekeeping, every minute)
    writes the latest pending change again, and the first failure of a run of them is alerted
    (`feed_health.unwritten`) once."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        clock: Clock = SYSTEM_CLOCK,
        submit: Callable[[Callable[[], None]], None] | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._submit = submit or (lambda work: TriggerService._submit(work, HEALTH_POOL))
        self._notifier = notifier
        self._last: datetime | None = None
        self._seq = 0
        self._written = 0
        self._latest: tuple[bool, datetime, int] | None = None
        self._alerted = False
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
        self._latest = (stale, at, seq)
        self._submit(lambda: self.write(stale, at, seq))

    def write(self, stale: bool, at: datetime, seq: int) -> bool:
        """Write change `seq` unless a newer one is written; True when it is (or a newer one)."""
        with self._lock:
            if seq <= self._written:
                return True  # a newer change is already written
            if not self._write(stale, at):
                self._alert_once(stale)
                return False
            self._written = seq
            self._alerted = False
            return True

    def retry(self) -> bool:
        """Write the latest change again when it is still pending; True when nothing is."""
        latest = self._latest
        return latest is None or self.write(*latest)

    def _alert_once(self, stale: bool) -> None:
        if self._alerted or self._notifier is None:
            return
        self._alerted = True
        try:
            self._notifier.alert(
                ALERT_FEED_HEALTH_UNWRITTEN,
                trade_date_of(utc_now(self._clock)),
                {"state": "stale" if stale else "fresh"},
            )
        except Exception:
            logger.exception("alert %s not sent", ALERT_FEED_HEALTH_UNWRITTEN)

    def _write(self, stale: bool, at: datetime) -> bool:
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
            return False
        return True
