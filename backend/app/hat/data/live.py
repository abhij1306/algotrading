"""The live-data engine: ticks in, 5-minute bars, staleness and gap capture out (02-data §11).

HAT reads ticks from its own live feed (`feed.py`) and never opens a socket of its own
(HAT-DATA-110, HAT-OVR-006): `LiveData.attach` registers `on_tick` with the feed's
`add_tick_listener`, and that is the only tick input. PR 17 does the attaching in the lifespan;
until then nothing constructs `LiveData`. The hook runs on the event-loop thread, so everything
here that touches state runs on that thread; the periodic `run` loop is a task on the same loop,
and the blocking work (the Parquet write, the event insert) is handed to a worker thread.

Every LTP HAT acts on comes from this one tick input, dated with its exchange time (`Quote`,
PR 51): a Take, a placement or an exit uses it only through `usable_price`, which refuses a price
the stale threshold old (HAT-DATA-116) or outside the day's price band. No quote is ever read
from the broker's REST API, so the loop spends none of the call budget (HAT-BRK-082).

A symbol first seen after the open is seeded from the exchange's day high and low on its tick
(`DAY_HIGH_FIELD`, `DAY_LOW_FIELD`, PR 39) only when `exchange_range` is on: the field names are
not yet checked against a recorded tick, so the flag stays off and such a symbol has no opening
range or day low until they are (HAT-DATA-114). A watched symbol silent for long is reported by
`silence` (an alert and a resubscribe, never a kill).

Nothing here runs unless HAT is enabled: the wiring (PR 17) builds a `LiveData` only when
`HAT_ENABLED` is true, so with HAT off the feed has no listener and behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.hat.clock import SYSTEM_CLOCK, Clock

from ..events import record
from ..notify import Notifier
from ..time import trade_date_of
from .bars import Bar, BarBuilder, Tick
from .bars_store import BarStore, Coverage
from .preopen import PreopenGaps
from .staleness import STALE_AFTER, FeedHealth, SilenceWatch, StalenessMonitor

__all__ = [
    "ALERT_SYMBOL_SILENT",
    "DAY_HIGH_FIELD",
    "DAY_LOW_FIELD",
    "PRICE_OUT_OF_BAND",
    "PRICE_STALE",
    "PRICE_UNKNOWN",
    "EventFeedHealth",
    "LiveData",
    "Quote",
    "TickFeed",
    "SymbolProvider",
    "off_loop",
    "run_listener_call",
    "silence_alert",
    "threadsafe_provider",
    "tick_from_payload",
    "usable_price",
]

logger = logging.getLogger(__name__)

TICK_TIME_FIELDS = ("exch_feed_time", "last_traded_time")
ALERT_SYMBOL_SILENT = "symbol_silent"
# [A] The exchange's day high and low so far on a tick; unverified (no recorded tick yet).
DAY_HIGH_FIELD = "high_price"
DAY_LOW_FIELD = "low_price"
STEP_SECONDS = 1.0
EVENT_WRITE_ATTEMPTS = 3

BarListener = Callable[[Bar], None]

PRICE_UNKNOWN = "price_unknown"  # no tick of the symbol yet
PRICE_STALE = "stale_price"  # its last tick is the stale threshold old
PRICE_OUT_OF_BAND = "price_out_of_band"  # outside the day's price band: not a real print


@dataclass(frozen=True)
class Quote:
    """A symbol's last traded price and the exchange time it traded at (the tick's own time,
    else its arrival)."""

    price: Decimal
    at: datetime


def usable_price(
    quote: Quote | None,
    now: datetime,
    *,
    lower: Decimal | None = None,
    upper: Decimal | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> tuple[Decimal | None, str | None]:
    """The price a Take, a placement or an exit may act on at `now`, or None and why (PR 51).

    A quote at least `stale_after` old is stale, as a feed with no tick in that time is
    (HAT-DATA-116); so is one stamped that far ahead of `now` (a bad exchange time). A price
    below the day's lower band price or above its upper one cannot have traded and is refused;
    a bound that is not known (no band row, an F&O name) is not checked."""
    if quote is None:
        return None, PRICE_UNKNOWN
    if now - quote.at >= stale_after or quote.at - now >= stale_after:
        return None, PRICE_STALE
    if (lower is not None and quote.price < lower) or (upper is not None and quote.price > upper):
        return None, PRICE_OUT_OF_BAND
    return quote.price, None


class TickFeed(Protocol):
    """The listener hook of `LiveFeed`."""

    def add_tick_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]: ...


def _decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _exchange_time(payload: Mapping[str, Any]) -> datetime | None:
    for name in TICK_TIME_FIELDS:
        value = _decimal(payload.get(name))
        if value is not None and value > 0:
            try:
                return datetime.fromtimestamp(float(value), UTC)
            except (OverflowError, OSError, ValueError):
                return None
    return None


def tick_from_payload(
    payload: Mapping[str, Any], arrival: datetime, last_volume: dict[str, tuple[date, int]]
) -> Tick | None:
    """A `Tick` from a normalized feed payload, or None when it has no symbol or price.

    The exchange time is the payload's, else the arrival time. The volume of the print is the
    change in the payload's cumulative day volume since the symbol's previous tick (never
    negative); the first tick seen that day contributes its own `last_traded_qty` (or 0), so
    volume that traded before HAT subscribed is not put into the current bar. A tick with no
    usable cumulative volume contributes its `last_traded_qty`. `last_volume` carries the
    previous cumulative volume per symbol with the trade date it belongs to, so a new day never
    subtracts yesterday's total.
    """
    symbol = payload.get("symbol")
    price = _decimal(payload.get("ltp"))
    if not isinstance(symbol, str) or not symbol or price is None or price <= 0:
        return None
    when = _exchange_time(payload) or arrival
    day = trade_date_of(when)
    cumulative = _decimal(payload.get("volume"))
    traded = _decimal(payload.get("last_traded_qty"))
    own = int(traded) if traded is not None and traded > 0 else 0
    qty = own
    if cumulative is not None and cumulative >= 0:
        total = int(cumulative)
        previous = last_volume.get(symbol)
        if previous is not None and previous[0] == day and total >= previous[1]:
            qty = total - previous[1]
        last_volume[symbol] = (day, total)
    return Tick(symbol, when, price, qty)


def run_listener_call(call: Callable[[], None], what: str) -> None:
    """Run a listener callback; its failure is logged and never reaches the caller."""
    try:
        call()
    except Exception:
        logger.exception("%s failed", what)


def off_loop(work: Callable[[], None]) -> None:
    """Run blocking `work` on a worker thread when called on the loop, else inline."""
    try:
        asyncio.get_running_loop().run_in_executor(None, work)
    except RuntimeError:  # no loop (a plain thread): run inline
        work()


def silence_alert(notifier: Notifier) -> Callable[[str, datetime], None]:
    """`SilenceWatch`'s alert as the health alert `symbol_silent` for the symbol, sent off the
    loop (a notifier blocks on its transport)."""

    def alert(symbol: str, now: datetime) -> None:
        day = trade_date_of(now)
        off_loop(lambda: notifier.alert(ALERT_SYMBOL_SILENT, day, {"symbol": symbol}))

    return alert


class EventFeedHealth:
    """`FeedHealth` that records `feed.stale` in the event log and tells `on_change` (the health
    kill) at once. The insert runs on a worker thread, because the monitor is driven from the
    loop; `on_change` is called synchronously, so the stale state takes effect immediately."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._on_change = on_change

    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None:
        self._notify(True)
        silent = None if silent_for is None else int(silent_for.total_seconds())
        self._submit(lambda: self._write(now, silent))

    def feed_recovered(self, now: datetime) -> None:
        del now  # the protocol passes it; recovery writes no event
        self._notify(False)

    def _notify(self, stale: bool) -> None:
        change = self._on_change
        if change is not None:
            run_listener_call(lambda: change(stale), "feed health change")

    @staticmethod
    def _submit(work: Callable[[], None]) -> None:
        off_loop(work)

    def _write(self, now: datetime, silent_seconds: int | None) -> None:
        day = trade_date_of(now)
        for attempt in range(1, EVENT_WRITE_ATTEMPTS + 1):
            try:
                with self._session_factory() as session:
                    record(
                        session,
                        aggregate_type="session",
                        aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hat:feed:{day.isoformat()}"),
                        event_type="feed.stale",
                        actor="engine",
                        payload={"silent_seconds": silent_seconds},
                        occurred_at=now,
                    )
                    session.commit()
                return
            except Exception:
                logger.exception("recording feed.stale failed (attempt %d)", attempt)


class LiveData:
    """Ticks to bars. Single-threaded by contract: the loop thread."""

    def __init__(
        self,
        *,
        health: FeedHealth,
        is_open: Callable[[datetime], bool],
        clock: Clock = SYSTEM_CLOCK,
        builder: BarBuilder | None = None,
        store: BarStore | None = None,
        instrument_ids: Callable[[], Mapping[str, uuid.UUID]] = dict,
        exchange_range: bool = False,
        silence: SilenceWatch | None = None,
        watched: Callable[[], Collection[str]] = frozenset,
    ) -> None:
        self._clock = clock
        self.builder = builder or BarBuilder()
        self.staleness = StalenessMonitor(health, is_open)
        self.silence = silence
        self._watched = watched
        self._exchange_range = exchange_range
        self.gaps = PreopenGaps()
        self._store = store
        self._instrument_ids = instrument_ids
        self._last_volume: dict[str, tuple[date, int]] = {}
        self._bar_listeners: list[BarListener] = []
        self._tick_listeners: list[Callable[[Tick], None]] = []
        self._unsaved: list[Bar] = []
        # per day, the builder's coverage at the last step, taken on the loop thread for
        # `persist` (a worker thread), and the coverage `persist` last stored; each is replaced
        # whole by its one writer, never mutated in place
        self._coverage: dict[date, tuple[frozenset[str], datetime | None, datetime | None]] = {}
        self._stored: dict[date, tuple[frozenset[str], datetime | None, datetime | None]] = {}
        self._detach: Callable[[], None] | None = None

    def attach(self, feed: TickFeed) -> None:
        """The one tick input: the shared feed's listener hook (HAT-DATA-110)."""
        if self._detach is None:
            self._detach = feed.add_tick_listener(self.on_tick)

    def detach(self) -> None:
        if self._detach is not None:
            self._detach()
            self._detach = None

    def add_bar_listener(self, listener: BarListener) -> None:
        """Called with each final bar, in order (the trigger monitor of PR 15)."""
        self._bar_listeners.append(listener)

    def restore(self, day: date) -> int:
        """A restart mid-session: the day's final bars and coverage already stored give the bar
        builder back its day volumes and, for the symbols watched from the open, its opening
        ranges (blocking: reads the store). Returns the bars read."""
        if self._store is None:
            return 0
        symbols = {str(i): s for s, i in self._instrument_ids().items()}
        bars = [
            Bar(symbol, row.bar_start, row.open, row.high, row.low, row.close, row.volume)
            for row in self._store.read(day)
            if (symbol := symbols.get(row.instrument_id)) is not None
        ]
        coverage = self._store.read_coverage(day)
        from_open = [symbols[i] for i in coverage.from_open if i in symbols]
        return self.builder.restore(bars, from_open, coverage.gap_from, coverage.seen_until)

    def add_tick_listener(self, listener: Callable[[Tick], None]) -> None:
        """Called with each tick that has a symbol and a positive price (the last-price book)."""
        self._tick_listeners.append(listener)

    def on_tick(self, payload: dict[str, Any]) -> None:
        now = self._clock.now()
        self._note_gap(now)
        self.staleness.on_tick(now)
        self.gaps.observe(payload, now)
        tick = tick_from_payload(payload, now, self._last_volume)
        if tick is not None:
            self.builder.on_tick(tick, now)
            if self._exchange_range:
                self._seed(payload, tick, now)
            if self.silence is not None:
                self.silence.on_tick(tick.symbol, now)
            for listener in self._tick_listeners:
                run_listener_call(lambda fn=listener: fn(tick), "tick listener")

    def _seed(self, payload: Mapping[str, Any], tick: Tick, now: datetime) -> None:
        """Seed the builder from the tick's exchange day high and low, when it has both and its
        own exchange time (the fields are as of that time, not the arrival)."""
        high = _decimal(payload.get(DAY_HIGH_FIELD))
        low = _decimal(payload.get(DAY_LOW_FIELD))
        if high is not None and low is not None and _exchange_time(payload) is not None:
            self.builder.seed(tick, high, low, now)

    def step(self) -> list[Bar]:
        """One timer step: finalise bars, notify bar listeners in order, re-check staleness and
        each watched symbol's silence."""
        now = self._clock.now()
        self.staleness.check(now)
        if self.silence is not None:
            self.silence.check(now, self._watched(), self.staleness.stale)
        self._note_gap(now)
        finals = self.builder.advance(now)
        for bar in finals:
            for listener in self._bar_listeners:
                run_listener_call(lambda b=bar, fn=listener: fn(b), "bar listener")
        if self._store is not None:  # without a store nothing would ever clear the list
            days = {trade_date_of(bar.start) for bar in finals} | {trade_date_of(now)}
            snapshot = {d: self.builder.coverage(d) for d in days}
            if any(self._coverage.get(d) != c for d, c in snapshot.items()):
                # a gap marked between bars is stored too, so a restart before the next bar
                # cannot restore an opening range the gap took away
                self._coverage = snapshot
            self._unsaved.extend(finals)
        return finals

    @property
    def coverage_pending(self) -> bool:
        """Coverage taken by a step that `persist` has not stored yet."""
        return self._coverage != self._stored

    def _note_gap(self, now: datetime) -> None:
        """A stale feed inside the session is a gap from its last tick (or the open): prints may
        be missing from then on, so the builder stops publishing the day low (PR 22). Before
        this process's first tick nothing was watched, so there is no gap to mark: a symbol
        first seen after the open is not watched from it, and a restart is marked by
        `restore`."""
        last = self.staleness.last_tick
        if last is None or not self.staleness.is_stale(now):
            return
        since = max(last, self.builder.session_start(trade_date_of(now)))
        if now - since >= self.staleness.stale_after:
            self.builder.mark_gap(since)

    @property
    def pending_bars(self) -> int:
        """Final bars waiting to be written."""
        return len(self._unsaved)

    def persist(self) -> int:
        """Write the final bars not yet stored (blocking; run it off the loop). A failure keeps
        them for the next attempt: the write merges by key, so a retry changes nothing twice."""
        if self._store is None or not (self._unsaved or self.coverage_pending):
            return 0
        pending = list(self._unsaved)
        coverage = self._coverage
        ids = self._instrument_ids()
        try:
            written = self._store.write(pending, ids) if pending else 0
            for day, (watched, gap, seen) in sorted(coverage.items()):  # after their bars
                found = frozenset(str(ids[s]) for s in watched if s in ids)
                self._store.write_coverage(day, Coverage(found, gap, seen))
            self._stored = coverage
        except Exception:
            logger.exception("bars_5m write failed; will retry")
            return 0
        del self._unsaved[: len(pending)]
        return written

    async def run(self, interval: float = STEP_SECONDS) -> None:
        while True:
            try:
                if self.step() or self._unsaved or self.coverage_pending:
                    await asyncio.to_thread(self.persist)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("live data step failed; continuing")
            await asyncio.sleep(interval)


class SymbolProvider(Protocol):
    """The live service's async subscribe and unsubscribe."""

    async def subscribe(self, symbols: list[str]) -> None: ...

    async def unsubscribe(self, symbols: list[str]) -> None: ...


def threadsafe_provider(
    service: SymbolProvider, loop: asyncio.AbstractEventLoop
) -> tuple[Callable[[list[str]], None], Callable[[list[str]], None]]:
    """(subscribe, unsubscribe) callables that hand the provider call to `loop`, so a job body on
    a worker thread can use them (CLAUDE.md: SDK and worker threads never touch the loop)."""

    def submit(method: Callable[[list[str]], Any], symbols: list[str]) -> None:
        future = asyncio.run_coroutine_threadsafe(method(symbols), loop)
        future.add_done_callback(
            lambda f: (
                f.exception() and logger.error("live provider call failed", exc_info=f.exception())
            )
        )

    return (lambda s: submit(service.subscribe, s)), (lambda s: submit(service.unsubscribe, s))
