"""The live prices of the trading day (PR 17, PR 51): what the trigger, the router and the exits
act on while the market is open.

- `PriceBook`: the feed's last trade per instrument with its exchange time (a `LiveData` tick
  listener, loop thread).
- `LivePrices`: the price a Take, a placement or an exit acts on: the last trade, refused when
  the stale threshold old or outside the day's price band (`data.live.usable_price`, PR 51); an
  exit view also takes an illiquid name's older last trade while the feed is fresh
  (`exit_price`, QA15).
- `SaleBands`: the day's lower band of an instrument for its sales (PR 48).
- `stale_feed`: whether the feed is stale now; before a first tick in an open session it is.

An unknown price never fires anything.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.bars import Tick
from ..data.live import Quote, exit_price, usable_price
from ..data.staleness import StalenessMonitor
from ..engine.trigger import (
    DailyInputs,
    locked_lower_circuit,
    lower_circuit_price,
    upper_circuit_price,
)
from ..models import HatInstrument
from ..time import trade_date_of, utc_now
from .market import DailyBook

__all__ = ["LivePrices", "PriceBook", "SaleBands", "band_bounds", "stale_feed"]

logger = logging.getLogger(__name__)


class PriceBook:
    """The last traded price per instrument from the feed, with the exchange time of its tick.
    `on_tick` runs on the loop thread with ticks `LiveData` already parsed (a symbol and a
    finite positive price); `quote` and `price` may be read from any thread (a dict read of an
    immutable value). `track_session` and `reset` replace the id map under a lock: the scheduler's
    jobs and the housekeeping track from different threads, and neither may drop the other's."""

    def __init__(self) -> None:
        self._prices: dict[str, Quote] = {}
        self._symbols: dict[uuid.UUID, str] = {}
        self._track = threading.Lock()

    def on_tick(self, tick: Tick) -> None:
        # A print older than the one kept (by exchange time) arrived late: keep the newer one.
        kept = self._prices.get(tick.symbol)
        if kept is None or tick.at >= kept.at:
            self._prices[tick.symbol] = Quote(tick.price, tick.at)

    def track_session(self, session: Session, instrument_ids: set[uuid.UUID]) -> None:
        """The instruments whose prices are read by id (the day's subscriptions)."""
        if instrument_ids:
            found = session.execute(
                select(HatInstrument.id, HatInstrument.symbol).where(
                    HatInstrument.id.in_(instrument_ids)
                )
            )
            rows = dict(found.all())
            with self._track:
                self._symbols = {**self._symbols, **rows}

    def tracks(self, instrument_id: uuid.UUID) -> bool:
        """Whether the instrument's price is read by id (`track_session` added it)."""
        return instrument_id in self._symbols

    def reset(self) -> None:
        """A new trading day: yesterday's prices and tracked instruments are not today's
        (`live.prepare` tracks the day's own right after)."""
        with self._track:
            self._prices = {}
            self._symbols = {}

    def quote(self, instrument_id: uuid.UUID) -> Quote | None:
        symbol = self._symbols.get(instrument_id)
        return None if symbol is None else self._prices.get(symbol)

    def price(self, instrument_id: uuid.UUID) -> Decimal | None:
        """The last traded price, unchecked: for display only (`/positions`)."""
        quote = self.quote(instrument_id)
        return None if quote is None else quote.price


def band_bounds(daily: DailyInputs) -> tuple[Decimal | None, Decimal | None]:
    """The lowest and highest price the day's band lets trade, a tick wider each way
    for the exchange's own rounding of the band; None for a bound not known (no band row, an
    F&O name, no previous close)."""
    slack = daily.tick if daily.tick is not None and daily.tick > 0 else Decimal(0)
    lower, upper = lower_circuit_price(daily), upper_circuit_price(daily)
    return (
        None if lower is None else lower - slack,
        None if upper is None else upper + slack,
    )


class SaleBands:
    """The day's lower band of an instrument for its sales (PR 48, `fills.SaleBand`): the lower
    circuit price from the stored band, and whether the last trade the feed gave is at it
    (the instrument is locked at its lower circuit). A locked instrument trades seldom, so its
    last trade is read whatever its age."""

    def __init__(self, quotes: PriceBook, book: DailyBook) -> None:
        self._quotes = quotes
        self._book = book

    def _daily(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> DailyInputs:
        return self._book.day(trade_date_of(now)).daily(session, instrument_id)

    def lower(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> Decimal | None:
        return lower_circuit_price(self._daily(session, instrument_id, now))

    def locked(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> bool | None:
        quote = self._quotes.quote(instrument_id)
        if quote is None:
            return None
        return locked_lower_circuit(quote.price, self._daily(session, instrument_id, now))


class LivePrices:
    """The live price a Take, a placement or an exit acts on (PR 51): the instrument's last
    trade from the feed, refused when it is the stale threshold old (HAT-DATA-116) or outside
    the day's price band. `checked` gives the price or None and the refusal's fixed code.
    `exit_price` is the exit views' (QA15): while `feed_fresh` says the feed is, a silent
    symbol's last trade of the day still counts."""

    def __init__(
        self,
        quotes: PriceBook,
        book: DailyBook,
        clock: Clock,
        feed_fresh: Callable[[], bool] = lambda: False,
    ) -> None:
        self._quotes = quotes
        self._book = book
        self._clock = clock
        self._feed_fresh = feed_fresh

    def _bounds(
        self, session: Session, instrument_id: uuid.UUID, at: datetime, quote: Quote | None
    ) -> tuple[Decimal | None, Decimal | None]:
        if quote is None:
            return None, None
        return band_bounds(self._book.day(trade_date_of(at)).daily(session, instrument_id))

    def checked(
        self, session: Session, instrument_id: uuid.UUID, now: datetime | None = None
    ) -> tuple[Decimal | None, str | None]:
        at = utc_now(self._clock) if now is None else now
        quote = self._quotes.quote(instrument_id)
        lower, upper = self._bounds(session, instrument_id, at, quote)
        price, refusal = usable_price(quote, at, lower=lower, upper=upper)
        if refusal is not None:
            logger.debug("live price of %s refused: %s", instrument_id, refusal)
        return price, refusal

    def price(self, session: Session, instrument_id: uuid.UUID) -> Decimal | None:
        return self.checked(session, instrument_id)[0]

    def limits(
        self, session: Session, instrument_id: uuid.UUID, now: datetime | None = None
    ) -> DailyInputs:
        """The instrument's `DailyInputs` of its trade date (the previous close, the tick and the
        stored band in force then): what a rotation's `refuse_near_limit` reads
        (`rotation.near_limit`), as the backtest reads them."""
        at = utc_now(self._clock) if now is None else now
        return self._book.day(trade_date_of(at)).daily(session, instrument_id)

    def exit_price(self, session: Session, instrument_id: uuid.UUID) -> Decimal | None:
        """The price an exit view acts on (`data.live.exit_price`); None when unknown."""
        at = utc_now(self._clock)
        quote = self._quotes.quote(instrument_id)
        lower, upper = self._bounds(session, instrument_id, at, quote)
        price, refusal = exit_price(
            quote, at, lower=lower, upper=upper, feed_fresh=self._feed_fresh()
        )
        if refusal is not None:
            logger.debug("exit price of %s refused: %s", instrument_id, refusal)
        return price


def stale_feed(staleness: StalenessMonitor, now: datetime) -> bool:
    """HAT-DATA-116 read at `now`, not the monitor's last transition: in an open session the feed
    is stale until a tick arrives, so a restart never reads a feed it has not heard from as fresh
    (an entry resumed or triggered then is refused `stale_feed`)."""
    return staleness.is_stale(now)
