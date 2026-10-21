"""5-minute bars from ticks (HAT-DATA-113, 114, 115).

`BarBuilder` is pure and synchronous: it takes ticks and the current instant and keeps no clock,
thread or I/O of its own, so a property can drive any tick sequence. It is single-threaded by
contract: the live service calls it only from the event-loop thread.

Rules (02-data §11, "Bar builder rules"):

- A bar starts at the session open (09:15 IST) plus a whole number of 5-minute widths. A tick at
  exchange time `t` belongs to exactly the bar starting at `open + 5 min * floor((t - open) /
  5 min)`. The session is half-open, `[open, close)`: a print stamped at or after the close, or
  before the open, belongs to no bar.
- A bar is final at `end + 2 s`. A tick that arrives (`now`) at or after that instant is *late*:
  it is counted and dropped, so a final bar never changes. A tick that arrives earlier is in
  time even when it is out of order: open and close are chosen by exchange time (arrival order
  breaks ties), so the bar does not depend on arrival order.
- A window with no tick produces no bar.
- The opening range is the high and low of the first bar of a session, fixed when that bar is
  final and never changed afterwards.
- Volume pace is cumulative day volume over the fraction of the session elapsed. The day volume
  counts every in-session tick, late ones included (the tape did trade them); bars exclude late
  ticks (they are immutable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from app.utils.clock import require_aware
from app.utils.market_hours import IST, SESSION_CLOSE, SESSION_OPEN

__all__ = [
    "BAR_FINAL_LAG",
    "BAR_WIDTH",
    "Bar",
    "BarBuilder",
    "Tick",
    "TickOutcome",
    "volume_pace",
]

BAR_WIDTH = timedelta(minutes=5)
BAR_FINAL_LAG = timedelta(seconds=2)


@dataclass(frozen=True)
class Tick:
    """One print: DB-format symbol, exchange time (aware), price, and the volume of this print."""

    symbol: str
    at: datetime
    price: Decimal
    qty: int = 0

    def __post_init__(self) -> None:
        require_aware(self.at)
        if self.qty < 0:
            raise ValueError("a tick's quantity cannot be negative")


@dataclass(frozen=True)
class Bar:
    """A 5-minute bar. `start` is aware, in IST; the bar covers `[start, start + 5 min)`."""

    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def end(self) -> datetime:
        return self.start + BAR_WIDTH

    @property
    def final_at(self) -> datetime:
        return self.end + BAR_FINAL_LAG


class TickOutcome(StrEnum):
    ACCEPTED = "accepted"
    LATE = "late"  # its bar was already final: counted, never applied
    OUTSIDE_SESSION = "outside_session"


@dataclass
class _OpenBar:
    start: datetime
    open_key: tuple[datetime, int]
    open: Decimal
    close_key: tuple[datetime, int]
    close: Decimal
    high: Decimal
    low: Decimal
    volume: int

    def add(self, tick: Tick, seq: int) -> None:
        key = (tick.at, seq)
        if key < self.open_key:
            self.open_key, self.open = key, tick.price
        if key > self.close_key:
            self.close_key, self.close = key, tick.price
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.volume += tick.qty


@dataclass
class _DayState:
    volume: int = 0
    ticks: int = 0


@dataclass
class BarBuilder:
    """Builds 5-minute bars; see the module docstring for the rules."""

    session_open: time = SESSION_OPEN
    session_close: time = SESSION_CLOSE
    late_ticks: int = 0
    _open_bars: dict[tuple[str, datetime], _OpenBar] = field(default_factory=dict)
    _opening: dict[tuple[str, date], tuple[Decimal, Decimal]] = field(default_factory=dict)
    _days: dict[tuple[str, date], _DayState] = field(default_factory=dict)
    _seq: int = 0

    def __post_init__(self) -> None:
        if self.session_open >= self.session_close:
            raise ValueError("the session must open before it closes")

    def session_start(self, day: date) -> datetime:
        return datetime.combine(day, self.session_open, tzinfo=IST)

    def session_end(self, day: date) -> datetime:
        return datetime.combine(day, self.session_close, tzinfo=IST)

    def bar_start(self, at: datetime) -> datetime | None:
        """Start of the bar a print at `at` belongs to, or None outside `[open, close)`."""
        local = require_aware(at).astimezone(IST)
        opens = self.session_start(local.date())
        if not opens <= local < self.session_end(local.date()):
            return None
        return opens + BAR_WIDTH * ((local - opens) // BAR_WIDTH)

    def on_tick(self, tick: Tick, now: datetime) -> TickOutcome:
        """Apply `tick`, which arrived at `now`. Never changes a bar that is already final."""
        require_aware(now)
        start = self.bar_start(tick.at)
        if start is None:
            return TickOutcome.OUTSIDE_SESSION
        day = start.date()
        state = self._days.setdefault((tick.symbol, day), _DayState())
        state.volume += tick.qty
        state.ticks += 1
        if start + BAR_WIDTH + BAR_FINAL_LAG <= now:
            self.late_ticks += 1
            return TickOutcome.LATE
        self._seq += 1
        bar = self._open_bars.get((tick.symbol, start))
        if bar is None:
            key = (tick.at, self._seq)
            self._open_bars[(tick.symbol, start)] = _OpenBar(
                start, key, tick.price, key, tick.price, tick.price, tick.price, tick.qty
            )
        else:
            bar.add(tick, self._seq)
        return TickOutcome.ACCEPTED

    def advance(self, now: datetime) -> list[Bar]:
        """Finalise every bar whose end plus 2 s has passed; each is returned exactly once,
        ordered by start then symbol. The first bar of a session also fixes the opening range."""
        require_aware(now)
        ready = sorted(
            (
                key
                for key, bar in self._open_bars.items()
                if bar.start + BAR_WIDTH + BAR_FINAL_LAG <= now
            ),
            key=lambda key: (key[1], key[0]),
        )
        finals: list[Bar] = []
        for key in ready:
            raw = self._open_bars.pop(key)
            bar = Bar(key[0], raw.start, raw.open, raw.high, raw.low, raw.close, raw.volume)
            finals.append(bar)
            day = bar.start.date()
            if bar.start == self.session_start(day):
                self._opening.setdefault((bar.symbol, day), (bar.high, bar.low))
        return finals

    def opening_range(self, symbol: str, day: date) -> tuple[Decimal, Decimal] | None:
        """(high, low) of the session's first bar once it is final; None before that, and for
        a symbol that had no tick in the first bar."""
        return self._opening.get((symbol, day))

    def day_volume(self, symbol: str, day: date) -> int:
        state = self._days.get((symbol, day))
        return state.volume if state else 0

    def volume_pace(self, symbol: str, at: datetime) -> Fraction | None:
        """Volume pace at `at`; None before the symbol's first tick of the session."""
        day = require_aware(at).astimezone(IST).date()
        state = self._days.get((symbol, day))
        if state is None or state.ticks == 0:
            return None
        return volume_pace(
            state.volume, at, session_open=self.session_open, session_close=self.session_close
        )

    def open_bar_count(self) -> int:
        return len(self._open_bars)


def volume_pace(
    cum_volume: int,
    at: datetime,
    *,
    session_open: time = SESSION_OPEN,
    session_close: time = SESSION_CLOSE,
) -> Fraction | None:
    """Cumulative day volume projected to the full session: `V / (elapsed / session length)`.

    Exact (a `Fraction`, never a float). None outside `(open, close]`, where no fraction of the
    session has elapsed yet or it is over.
    """
    if cum_volume < 0:
        raise ValueError("volume cannot be negative")
    local = require_aware(at).astimezone(IST)
    opens = datetime.combine(local.date(), session_open, tzinfo=IST)
    closes = datetime.combine(local.date(), session_close, tzinfo=IST)
    if not opens < local <= closes:
        return None
    micro = timedelta(microseconds=1)
    return Fraction(cum_volume * ((closes - opens) // micro), (local - opens) // micro)
