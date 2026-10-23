"""5-minute bars from ticks (HAT-DATA-113, 114, 115).

`BarBuilder` is pure and synchronous: it takes ticks and the current instant and keeps no clock,
thread or I/O of its own, so a property can drive any tick sequence. It is single-threaded by
contract: the live service calls it only from the event-loop thread.

Rules ("Bar builder rules"):

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
  final and never changed afterwards. It is published only for a symbol watched from the open:
  its first tick that day arrived at or before the session open, and no feed gap began before
  the first bar ended. Otherwise prints the builder never saw may hold the real range, so there
  is none (PR 22).
- The day low is the lowest low of the day's final bars, published on the same terms and only
  while no feed gap has been marked that day: a late subscription, a restart or a gap may have
  missed the low, and a stop under a partial low is too tight and oversized (PR 22).
- Volume pace is cumulative day volume over the fraction of the session elapsed. The day volume
  counts every in-session tick, late ones included (the tape did trade them); bars exclude late
  ticks (they are immutable).
- A restart mid-session `restore`s the day's stored final bars: their volume, and the opening
  ranges of the symbols the stored coverage says were watched from the open, for each symbol the
  builder has not seen that day (PR 17). The restart itself is a feed gap (PR 22).
- A symbol first seen late (a 09:17 gap-scan name, a restart, a feed gap) can be *seeded* from a
  tick that carries the exchange's day high and low so far (PR 39, behind a flag until the field
  names are checked against a recorded tick). The seed covers every print up to its exchange
  time, so what the builder missed before it no longer matters; only a feed gap that began at or
  after the seed arrived does. With a seed taken inside the first bar, the opening range is the
  seed's range widened by the first bar's own ticks; the day low is the seed's low or a lower
  final bar's. A seed is kept per (symbol, day) and replaced only after a gap, so a tick after
  the gap seeds again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from app.hat.clock import require_aware
from app.hat.data.sessions import IST, SESSION_CLOSE, SESSION_OPEN

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


@dataclass(frozen=True)
class _Seed:
    """The exchange's day high and low as of exchange time `at`, from a tick arriving at
    `arrival`."""

    arrival: datetime
    at: datetime
    high: Decimal
    low: Decimal

    @property
    def covered_until(self) -> datetime:
        """Prints up to here are in the seed or reach the builder as ticks: a gap that began at
        or after it spoils the seed. A delayed tick's exchange time is before its arrival, and a
        gap between the two hides prints the seed does not hold."""
        return min(self.at, self.arrival)


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
    _lows: dict[tuple[str, date], list[tuple[datetime, Decimal]]] = field(default_factory=dict)
    _watched: dict[tuple[str, date], datetime] = field(default_factory=dict)
    _gaps: dict[date, set[datetime]] = field(default_factory=dict)
    _seeds: dict[tuple[str, date], list[_Seed]] = field(default_factory=dict)
    _last_arrival: dict[date, datetime] = field(default_factory=dict)
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
        arrival_day = now.astimezone(IST).date()
        if arrival_day not in self._last_arrival:
            self._forget_before(arrival_day)
        self._watched.setdefault((tick.symbol, arrival_day), now)
        if now > self._last_arrival.get(arrival_day, now - BAR_WIDTH):
            self._last_arrival[arrival_day] = now
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
            self._note_low(bar, day)
            if bar.start == self.session_start(day):
                self._opening.setdefault((bar.symbol, day), (bar.high, bar.low))
        return finals

    def _note_low(self, bar: Bar, day: date) -> None:
        self._lows.setdefault((bar.symbol, day), []).append((bar.start, bar.low))

    def mark_gap(self, since: datetime) -> None:
        """Prints from `since` on may be missing (a feed gap, a restart): for the rest of that
        day no day low is published, nor an opening range if `since` is before the first bar's
        end."""
        day = require_aware(since).astimezone(IST).date()
        self._gaps.setdefault(day, set()).add(since)

    def _first_gap(self, day: date) -> datetime | None:
        gaps = self._gaps.get(day)
        return min(gaps) if gaps else None

    def seed(self, tick: Tick, high: Decimal, low: Decimal, now: datetime) -> bool:
        """Seed the symbol's day from `tick`, which arrived at `now` and carries the exchange's
        day `high` and `low` as of its exchange time. Refused (False) outside the session, when
        the three prices are not ordered `0 < low <= price <= high`, or while the day's last
        seed is still good (no feed gap began at or after its cover end)."""
        require_aware(now)
        start = self.bar_start(tick.at)
        if start is None or not Decimal(0) < low <= tick.price <= high:
            return False
        day = start.date()
        seeds = self._seeds.setdefault((tick.symbol, day), [])
        if seeds and not self._gap_after(day, seeds[-1].covered_until, None):
            return False
        seeds.append(_Seed(now, tick.at, high, low))
        return True

    def _gap_after(self, day: date, since: datetime, until: datetime | None) -> bool:
        """Whether a feed gap began in `[since, until)` that day (`until` None: no end)."""
        return any(since <= g and (until is None or g < until) for g in self._gaps.get(day, ()))

    def _seed_for(
        self, symbol: str, day: date, by: datetime, until: datetime | None
    ) -> _Seed | None:
        """The latest seed taken before exchange time `by`, if no gap began in `[its cover end,
        until)` (an earlier seed is spoiled by the same gap: seeds are kept in arrival order and
        a new one is taken only after a gap)."""
        for seed in reversed(self._seeds.get((symbol, day), ())):
            if seed.at < by:
                return None if self._gap_after(day, seed.covered_until, until) else seed
        return None

    def from_open(self, symbol: str, day: date) -> bool:
        """Whether the symbol's first tick that day arrived at or before the session open."""
        first = self._watched.get((symbol, day))
        return first is not None and first <= self.session_start(day)

    def coverage(self, day: date) -> tuple[frozenset[str], datetime | None, datetime | None]:
        """The symbols watched from the open of `day`, the start of its first feed gap, and how
        far the feed was seen, up to the first bar's end (all a restart needs: past it a restart
        gap only takes the day low, which any restart takes)."""
        opens = self.session_start(day)
        watched = frozenset(
            s for (s, d), first in self._watched.items() if d == day and first <= opens
        )
        last = self._last_arrival.get(day)
        seen = None if last is None else min(last, opens + BAR_WIDTH)
        return watched, self._first_gap(day), seen

    def restore(
        self,
        bars: Iterable[Bar],
        from_open: Iterable[str] = (),
        gap_from: datetime | None = None,
        seen_until: datetime | None = None,
    ) -> int:
        """Seed the day volume, the lows and (for the `from_open` symbols, which the process
        that stored the bars watched from the open) the opening range from final bars stored
        before a restart, for each (symbol, day) with no state yet (a second restore, or one
        after that symbol's first tick, changes nothing). `gap_from` is the stored first feed
        gap and `seen_until` how far that process saw the feed: the restart is a gap from there,
        or from the end of the last stored bar if earlier (from the open when unknown). Returns
        the bars applied."""
        bars = list(bars)
        watched = set(from_open)
        known = set(self._days)
        applied = 0
        for bar in bars:
            day = require_aware(bar.start).astimezone(IST).date()
            key = (bar.symbol, day)
            if key in known:
                continue
            state = self._days.setdefault(key, _DayState())
            state.volume += bar.volume
            state.ticks += 1
            self._note_low(bar, day)
            if bar.symbol in watched:
                self._watched.setdefault(key, self.session_start(day))
            if bar.start == self.session_start(day):
                self._opening.setdefault(key, (bar.high, bar.low))
            applied += 1
        if gap_from is not None:
            self.mark_gap(gap_from)
        if bars:
            restart = max(bar.end for bar in bars)
            opens = self.session_start(restart.astimezone(IST).date())
            self.mark_gap(opens if seen_until is None else min(restart, max(seen_until, opens)))
        return applied

    def _forget_before(self, day: date) -> None:
        """Drop the per-day state of earlier days (a process running across days)."""
        for table in (self._opening, self._days, self._lows, self._watched, self._seeds):
            for key in [k for k in table if k[1] < day]:
                del table[key]
        for table in (self._gaps, self._last_arrival):
            for key in [d for d in table if d < day]:
                del table[key]

    def _complete(
        self, symbol: str, day: date, until: datetime | None, seen_through: datetime
    ) -> bool:
        """Watched from the open with no feed gap before `until` (None: none at all that day),
        and a tick arrived at or after `seen_through`: a feed that went silent before a bar
        ended may have lost the bar's last prints, and only a silence of the stale threshold is
        marked as a gap, so until the feed is seen past the bar nothing is published."""
        gap = self._first_gap(day)
        clean = gap is None or (until is not None and gap >= until)
        return clean and self._seen(day, seen_through) and self.from_open(symbol, day)

    def _seen(self, day: date, through: datetime) -> bool:
        """Whether a tick (any symbol) arrived at or after `through` that day."""
        last = self._last_arrival.get(day)
        return last is not None and last >= through

    def opening_range(self, symbol: str, day: date) -> tuple[Decimal, Decimal] | None:
        """(high, low) of the session's first bar once it is final; None before that, for a
        symbol that had no tick in the first bar, and for one not watched from the open or with
        a feed gap before the first bar's end (fail closed), unless a seed taken inside the first
        bar with no gap after it before the bar's end covers what was missed: then the seed's
        range widened by the bar's."""
        first_end = self.session_start(day) + BAR_WIDTH
        opening = self._opening.get((symbol, day))
        if opening is None or self._complete(symbol, day, first_end, first_end):
            return opening
        seed = self._seed_for(symbol, day, first_end, first_end)
        if seed is None or not self._seen(day, first_end):
            return None
        return max(seed.high, opening[0]), min(seed.low, opening[1])

    def day_low(self, symbol: str, day: date, through: datetime | None = None) -> Decimal | None:
        """The lowest low of the day's final bars (those starting at or before `through`, when
        given); None unless the symbol was watched from the open, no feed gap was marked that
        day and the feed was seen past the last of those bars (fail closed), unless a seed taken
        inside those bars with no gap after it covers what was missed: then the lower of its low
        and theirs."""
        lows = [
            (start, low)
            for start, low in self._lows.get((symbol, day), ())
            if through is None or start <= through
        ]
        if not lows:
            return None
        covered = max(start for start, _ in lows) + BAR_WIDTH
        if not self._complete(symbol, day, None, covered):
            seed = self._seed_for(symbol, day, covered, None)
            if seed is None or not self._seen(day, covered):
                return None
            lows.append((seed.at, seed.low))
        return min(low for _, low in lows)

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
