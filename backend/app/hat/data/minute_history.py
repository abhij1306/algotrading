"""Optional Fyers minute history for episodic-pivot backtests (HAT-DEC-I4, HAT-DATA-119).

The loader asks the broker's history endpoint for 1-minute candles, aggregates them to the
5-minute bars of `bars_5m` and writes them with `BarStore`. Without it the episodic-pivot
playbook is not backtested (09 §1): daily bars cannot show an opening range.

A 1-minute candle stands for the prints of its minute: the open at the minute's start, then the
high and the low, then the close in its last second, the minute's volume carried by the open.
`candle_prints` gives those prints; `aggregate` builds 5-minute bars from candles directly, with
the live `BarBuilder`'s session rules and bucketing (a bar starts at the open plus a whole number
of 5-minute widths; nothing outside `[open, close)`), so its bars equal what the builder makes of
the same prints (PBT `bars.history_matches_builder`).

The SDK call is injected (`HistoryCall`, the shape of `fyersModel.FyersModel.history`): this
module never logs in, never reads a token and is never called by tests with a real client. A
response that is not `ok` raises `MinuteHistoryError` with a fixed code, never the broker's text.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ..errors import HatError
from .bars import Bar, BarBuilder, Tick
from .bars_store import BarStore

__all__ = [
    "CHUNK_DAYS",
    "HistoryCall",
    "MinuteCandle",
    "MinuteHistoryError",
    "aggregate",
    "candle_prints",
    "load_minute_history",
    "parse_candles",
]

CHUNK_DAYS = 100  # the most days of 1-minute candles one history request may span
_MINUTE = timedelta(minutes=1)
_PLACES = Decimal("0.0001")

HistoryCall = Callable[[dict[str, str]], Mapping[str, object]]


class MinuteHistoryError(HatError):
    """The history could not be loaded; `code` is a fixed word and carries no broker text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MinuteCandle:
    """One 1-minute candle; `at` is the minute's start (aware)."""

    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _price(raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise MinuteHistoryError("history_bad_candle")
    value = Decimal(repr(raw)).quantize(_PLACES)
    if value <= 0:
        raise MinuteHistoryError("history_bad_candle")
    return value


def parse_candles(raw: Iterable[Sequence[object]]) -> list[MinuteCandle]:
    """Candles from the history response's `candles` ([epoch seconds, open, high, low, close,
    volume] each), oldest first. A malformed or inconsistent candle raises; nothing is guessed."""
    found: dict[datetime, MinuteCandle] = {}
    for row in raw:
        if len(row) != 6 or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise MinuteHistoryError("history_bad_candle")
        volume = row[5]
        if isinstance(volume, float) and volume.is_integer():
            volume = int(volume)
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise MinuteHistoryError("history_bad_candle")
        open_, high, low, close = (_price(v) for v in row[1:5])
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise MinuteHistoryError("history_bad_candle")
        at = datetime.fromtimestamp(row[0], tz=UTC)
        found[at] = MinuteCandle(at, open_, high, low, close, volume)
    return [found[at] for at in sorted(found)]


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


def _chunks(start: date, end: date) -> list[tuple[date, date]]:
    spans = []
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=CHUNK_DAYS - 1))
        spans.append((first, last))
        first = last + timedelta(days=1)
    return spans


def load_minute_history(
    history: HistoryCall,
    store: BarStore,
    instruments: Mapping[str, uuid.UUID | str],
    start: date,
    end: date,
) -> int:
    """Fetch the 1-minute history of each broker symbol in `instruments` (broker symbol ->
    instrument id) for `start..end`, aggregate it and merge it into `bars_5m`. Returns the rows
    written. Nothing is written unless every symbol loaded. Safe to re-run: `BarStore.write`
    keeps a bar that did not change."""
    if start > end:
        raise MinuteHistoryError("history_bad_period")
    bars: dict[str, list[Bar]] = {}
    for symbol in sorted(instruments):
        candles: list[MinuteCandle] = []
        for first, last in _chunks(start, end):
            response = history(
                {
                    "symbol": symbol,
                    "resolution": "1",
                    "date_format": "1",
                    "range_from": first.isoformat(),
                    "range_to": last.isoformat(),
                    "cont_flag": "1",
                }
            )
            if not isinstance(response, Mapping) or response.get("s") != "ok":
                raise MinuteHistoryError("history_failed")
            raw = response.get("candles") or []
            if not isinstance(raw, list):
                raise MinuteHistoryError("history_bad_candle")
            candles.extend(parse_candles(raw))
        bars[symbol] = aggregate(symbol, candles)
    # Every symbol is fetched before anything is written: a failure leaves the store unchanged.
    return sum(store.write(bars[s], {s: instruments[s]}) for s in sorted(bars))
