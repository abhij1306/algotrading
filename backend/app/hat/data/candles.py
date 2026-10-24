"""Minute-candle policy (audit D1, D2, D9): a Fyers history answer's 1-minute candles parsed and
screened, and turned into the prints and 5-minute bars the bar builder keeps. Pure: no I/O.

`parse_candles` sorts an answer's rows into candles and rejected rows, each rejected row under
its rule (`REJECT_RULES`); `screen` drops every 5-minute bucket a rejected candle touches;
`candle_prints` and `aggregate` give a candle's prints and the 5-minute bars of a day's candles.
The minute-history download (`minute_history.py`) is their caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..money import MONEY_SCALE, exact
from .bars import Bar, BarBuilder, Tick

__all__ = [
    "REJECT_RULES",
    "MinuteCandle",
    "Parsed",
    "Rejected",
    "aggregate",
    "candle_prints",
    "parse_candles",
    "screen",
]

_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class MinuteCandle:
    """One 1-minute candle; `at` is the minute's start (aware)."""

    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


# The rules a candle can break, each its own ledger code (audit D2): not a 6-item row, an
# unreadable epoch, a volume that is not a whole number >= 0, a price that is not a positive
# number, or low <= open, close <= high broken.
REJECT_RULES = ("shape", "epoch", "volume", "price", "ohlc")


@dataclass(frozen=True)
class Rejected:
    """A candle the policy refused: the rule, its minute (None when its epoch is unreadable)
    and the minutes whose 5-minute buckets it removes: its own, or for an unreadable epoch the
    readable candles on either side of it in the answer."""

    rule: str
    at: datetime | None
    voids: tuple[datetime, ...]


@dataclass(frozen=True)
class Parsed:
    """One answer's candles (oldest first, one per minute), the rejected ones in answer order,
    the most rejected candles in a row (counting the run the previous answer ended with) and
    the run this one ends with."""

    candles: list[MinuteCandle]
    rejected: list[Rejected]
    longest_run: int = 0
    trailing_run: int = 0


def _price(raw: object) -> Decimal | None:
    """A candle price: a JSON number (never text), positive at the store's 4 places."""
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return None
    return exact(raw, floats=True, positive=True, places=MONEY_SCALE)


def _epoch(raw: object) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _volume(raw: object) -> int | None:
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _classify(row: object) -> MinuteCandle | tuple[str, datetime | None]:
    """The candle `row` stands for, or the rule it breaks and its minute when readable."""
    if not isinstance(row, list | tuple) or len(row) != 6:
        located = isinstance(row, list | tuple) and len(row) > 0
        return ("shape", _epoch(row[0]) if located else None)
    at = _epoch(row[0])
    if at is None:
        return ("epoch", None)
    volume = _volume(row[5])
    if volume is None:
        return ("volume", at)
    open_, high, low, close = (_price(v) for v in row[1:5])
    if open_ is None or high is None or low is None or close is None:
        return ("price", at)
    if not low <= min(open_, close) <= max(open_, close) <= high:
        return ("ohlc", at)
    return MinuteCandle(at, open_, high, low, close, volume)


def parse_candles(raw: Iterable[object], carry: int = 0) -> Parsed:
    """The history response's `candles` ([epoch seconds, open, high, low, close, volume] each)
    sorted into candles and rejected rows (`REJECT_RULES`); `carry` is the run of rejected rows
    the previous answer of the symbol ended with, so a run across two answers counts whole.
    Content never raises: a bad row is rejected, never repaired or guessed."""
    outcomes = [_classify(row) for row in raw]
    minutes = [o.at if isinstance(o, MinuteCandle) else o[1] for o in outcomes]
    found: dict[datetime, MinuteCandle] = {}
    rejected: list[Rejected] = []
    run = longest = carry
    for i, outcome in enumerate(outcomes):
        if isinstance(outcome, MinuteCandle):
            found[outcome.at] = outcome
            run = 0
            continue
        run += 1
        longest = max(longest, run)
        rule, at = outcome
        rejected.append(Rejected(rule, at, (at,) if at is not None else _neighbours(minutes, i)))
    return Parsed([found[at] for at in sorted(found)], rejected, longest, run)


def _neighbours(minutes: Sequence[datetime | None], i: int) -> tuple[datetime, ...]:
    """The readable minutes nearest before and after row `i`."""
    before = next((at for at in reversed(minutes[:i]) if at is not None), None)
    after = next((at for at in minutes[i + 1 :] if at is not None), None)
    return tuple(at for at in (before, after) if at is not None)


def screen(parsed: Parsed, builder: BarBuilder | None = None) -> list[MinuteCandle]:
    """The candles left once every 5-minute bucket that holds a rejected candle is removed: a
    missing bar is a legal state (a window without a candle has none), a wrong bar is not."""
    rules = builder or BarBuilder()
    void = {rules.bar_start(at) for r in parsed.rejected for at in r.voids} - {None}
    return [c for c in parsed.candles if rules.bar_start(c.at) not in void]


def candle_prints(symbol: str, candle: MinuteCandle) -> list[Tick]:
    """The prints a candle stands for: open at its start (carrying the volume), high, low, then
    the close in the minute's last second."""
    start = candle.at
    return [
        Tick(symbol, start, candle.open, candle.volume),
        Tick(symbol, start + timedelta(seconds=1), candle.high, 0),
        Tick(symbol, start + timedelta(seconds=2), candle.low, 0),
        Tick(symbol, start + _MINUTE - timedelta(seconds=1), candle.close, 0),
    ]


def aggregate(
    symbol: str, candles: Iterable[MinuteCandle], builder: BarBuilder | None = None
) -> list[Bar]:
    """The 5-minute bars of `candles`, ordered by start: open of the first candle, close of the
    last, the extreme high and low and the summed volume. A candle that starts outside the
    session belongs to no bar; a window without a candle has no bar."""
    rules = builder or BarBuilder()
    buckets: dict[datetime, list[MinuteCandle]] = {}
    for candle in sorted(candles, key=lambda c: c.at):
        start = rules.bar_start(candle.at)
        if start is not None:
            buckets.setdefault(start, []).append(candle)
    return [
        Bar(
            symbol,
            start,
            group[0].open,
            max(c.high for c in group),
            min(c.low for c in group),
            group[-1].close,
            sum(c.volume for c in group),
        )
        for start, group in sorted(buckets.items())
    ]
