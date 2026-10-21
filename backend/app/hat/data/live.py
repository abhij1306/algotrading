"""The live-data engine: ticks in, 5-minute bars, staleness and gap capture out (02-data §11).

HAT reads ticks from its own live feed (`feed.py`) and never opens a socket of its own
(HAT-DATA-110, HAT-OVR-006): `LiveData.attach` registers `on_tick` with the feed's
`add_tick_listener`, and that is the only tick input. The hook runs on the event-loop thread, so
everything here that touches state runs on that thread; the periodic `run` loop is a task on the
same loop, and the
blocking work (the Parquet write, the event insert) is handed to a worker thread.

Nothing here runs unless HAT is enabled: the wiring (PR 17) builds a `LiveData` only when
`HAT_ENABLED` is true, so with HAT off the feed has no listener and behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.hat.clock import SYSTEM_CLOCK, Clock

from ..events import record
from ..time import trade_date_of
from .bars import Bar, BarBuilder, Tick
from .bars_store import BarStore
from .preopen import PreopenGaps
from .staleness import FeedHealth, StalenessMonitor

__all__ = [
    "EventFeedHealth",
    "LiveData",
    "TickFeed",
    "SymbolProvider",
    "run_listener_call",
    "threadsafe_provider",
    "tick_from_payload",
]

logger = logging.getLogger(__name__)

TICK_TIME_FIELDS = ("exch_feed_time", "last_traded_time")
STEP_SECONDS = 1.0
EVENT_WRITE_ATTEMPTS = 3

BarListener = Callable[[Bar], None]


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
        try:
            asyncio.get_running_loop().run_in_executor(None, work)
        except RuntimeError:  # no loop (a plain thread): write inline
            work()

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
    ) -> None:
        self._clock = clock
        self.builder = builder or BarBuilder()
        self.staleness = StalenessMonitor(health, is_open)
        self.gaps = PreopenGaps()
        self._store = store
        self._instrument_ids = instrument_ids
        self._last_volume: dict[str, tuple[date, int]] = {}
        self._bar_listeners: list[BarListener] = []
        self._unsaved: list[Bar] = []
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

    def on_tick(self, payload: dict[str, Any]) -> None:
        now = self._clock.now()
        self.staleness.on_tick(now)
        self.gaps.observe(payload, now)
        tick = tick_from_payload(payload, now, self._last_volume)
        if tick is not None:
            self.builder.on_tick(tick, now)

    def step(self) -> list[Bar]:
        """One timer step: finalise bars, notify bar listeners in order, re-check staleness."""
        now = self._clock.now()
        self.staleness.check(now)
        finals = self.builder.advance(now)
        for bar in finals:
            for listener in self._bar_listeners:
                run_listener_call(lambda b=bar, fn=listener: fn(b), "bar listener")
        if self._store is not None:  # without a store nothing would ever clear the list
            self._unsaved.extend(finals)
        return finals

    @property
    def pending_bars(self) -> int:
        """Final bars waiting to be written."""
        return len(self._unsaved)

    def persist(self) -> int:
        """Write the final bars not yet stored (blocking; run it off the loop). A failure keeps
        them for the next attempt: the write merges by key, so a retry changes nothing twice."""
        if self._store is None or not self._unsaved:
            return 0
        pending = list(self._unsaved)
        try:
            written = self._store.write(pending, self._instrument_ids())
        except Exception:
            logger.exception("bars_5m write failed; will retry")
            return 0
        del self._unsaved[: len(pending)]
        return written

    async def run(self, interval: float = STEP_SECONDS) -> None:
        while True:
            try:
                if self.step() or self._unsaved:
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
