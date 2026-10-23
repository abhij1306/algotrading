"""Fyers minute history for episodic-pivot backtests (HAT-DEC-I4, HAT-DATA-119).

The loader asks the broker's history endpoint for 1-minute candles, aggregates them to the
5-minute bars of `bars_5m` and writes them with `BarStore`. Without it the episodic-pivot
playbook is not backtested: daily bars cannot show an opening range.

A 1-minute candle stands for the prints of its minute: the open at the minute's start, then the
high and the low, then the close in its last second, the minute's volume carried by the open.
`candle_prints` gives those prints; `aggregate` builds 5-minute bars from candles directly, with
the live `BarBuilder`'s session rules and bucketing (a bar starts at the open plus a whole number
of 5-minute widths; nothing outside `[open, close)`), so its bars equal what the builder makes of
the same prints (PBT `bars.history_matches_builder`).

Fyers' candles are adjusted for every split and bonus up to the day of the request; `bars_5m`
holds raw bars, like `bars_daily_raw` and the live builder (audit D1). Each answer is converted
with the stored corporate-action factors as of the day it was fetched, and each day's bars must
fit the raw daily bar of that day (`bars_basis`); a day that does not is recorded, not written.

A bad candle (`REJECT_RULES`, one code each) is rejected with its whole 5-minute bucket and
recorded in the ledger `bars_5m_rejects/` (audit D2); the symbol fails
(`history_too_many_rejects`) only above `max_reject_pct` of its candles or `max_reject_run` in
a row, and `--resume` does not ask again for a symbol that failed on its data (`--retry-failed`
does), only for one that failed in transport.

The limits are the settings' `history_*` (audit D3): the plan's depth, days per call, calls per
minute, and a day cap that leaves `history_reserve` of the broker's daily calls to live trading
(one Fyers app, one daily limit). Symbols are written `write_batch` at a time, one rewrite of
each day file per batch (audit D7); `Progress` keeps the day's call count, the symbols done and
the plan depth, so `--resume` continues a stopped run and a changed plan is seen. A -50 refusal
for the plan's depth clamps the start date to where the plan serves; it is not a failure.

The SDK call is injected (`HistoryCall`, the shape of `fyersModel.FyersModel.history`): only
`main` builds it, from HAT's own Fyers session through the data provider's `fyers_history`, and
tests never use a real client. A response that is not usable raises `MinuteHistoryError` with a
fixed code, never the broker's text.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..broker_limits import FYERS_CODE_RATE_LIMITED, PLAN_CALLS_PER_DAY
from ..clock import SYSTEM_CLOCK, Clock
from ..errors import HatError
from ..models import HatBrokerSymbol, HatInstrument, HatMarket
from ..models.strategy import BROKER_FYERS
from ..money import MONEY_SCALE, exact
from ..time import as_utc, trade_date_of
from .bars import Bar, BarBuilder, Tick
from .bars_basis import DailyRange, RawBasis, check_day, store_daily, to_raw
from .bars_store import BarStore, RejectRow
from .ingest import UNIVERSE_SERIES
from .run_report import save_report
from .sessions import IST
from .store import DataStore, atomic_replace

if TYPE_CHECKING:
    from ..settings import HatSettings

__all__ = [
    "CHUNK_DAYS",
    "PROGRESS_FILE",
    "REJECT_RULES",
    "CallPacer",
    "HistoryCall",
    "HistoryLimits",
    "HistoryReport",
    "MinuteCandle",
    "MinuteHistoryError",
    "Parsed",
    "Progress",
    "Rejected",
    "aggregate",
    "candle_prints",
    "history_universe",
    "load_minute_history",
    "main",
    "parse_candles",
    "rebase_stored",
    "screen",
]

CHUNK_DAYS = 100  # the most days of 1-minute candles one history request may span
_MINUTE = timedelta(minutes=1)
_WINDOW = timedelta(minutes=1)  # the per-minute call budget's window
_PLAN_REFUSAL = "historical data available for your plan"
LEGACY_PLAN_DAYS = 365  # the Standard plan's depth, which a progress file without one was under
# Codes that end the run (the rest fail one symbol): the day's call budget is spent (by our count
# or by Fyers'), or the plan keeps refusing the period.
# Fyers' -360 (the app's daily requests spent) is one of them (`history_rate_limited`).
_RUN_STOPS = frozenset({"history_daily_limit", "history_rate_limited", "history_plan_limit"})
# A symbol that failed on its data fails the same way again: a resume does not spend calls on it.
_DATA_FAILURES = frozenset(
    {"history_too_many_rejects", "history_bad_answer", "history_too_many_candles"}
)
PROGRESS_FILE = Path("minute_history") / "progress.json"  # under the HAT data root

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
    and the most rejected candles in a row."""

    candles: list[MinuteCandle]
    rejected: list[Rejected]
    longest_run: int = 0


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


def parse_candles(raw: Iterable[object]) -> Parsed:
    """The history response's `candles` ([epoch seconds, open, high, low, close, volume] each)
    sorted into candles and rejected rows (`REJECT_RULES`). Content never raises: a bad row is
    rejected, never repaired or guessed."""
    outcomes = [_classify(row) for row in raw]
    minutes = [o.at if isinstance(o, MinuteCandle) else o[1] for o in outcomes]
    found: dict[datetime, MinuteCandle] = {}
    rejected: list[Rejected] = []
    run = longest = 0
    for i, outcome in enumerate(outcomes):
        if isinstance(outcome, MinuteCandle):
            found[outcome.at] = outcome
            run = 0
            continue
        run += 1
        longest = max(longest, run)
        rule, at = outcome
        rejected.append(Rejected(rule, at, (at,) if at is not None else _neighbours(minutes, i)))
    return Parsed([found[at] for at in sorted(found)], rejected, longest)


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


# --- the download (D131: the Fyers Standard plan serves 1-minute candles for the last 365 days) --


@dataclass(frozen=True)
class HistoryLimits:
    """The Fyers plan's history limits and the loader's policy (audit D3, D2, D7), from the
    settings (`from_settings`) with command-line overrides: days per call, calls per minute and
    per IST day, how far back the plan serves intraday candles, the most candles one response
    may hold (by default 400 a day of the call, over the session's 375 minutes; a larger answer
    is refused, so one symbol never holds more than one response of candles at a time), the
    share of a symbol's candles (percent) and the run of them in a row that may be rejected
    before the symbol fails, and the symbols (or bars) written together."""

    chunk_days: int = CHUNK_DAYS
    per_minute: int = 50
    per_day: int = PLAN_CALLS_PER_DAY
    plan_days: int = LEGACY_PLAN_DAYS
    max_candles: int | None = None  # None: 400 x chunk_days
    max_clamps: int = 3  # plan-limit refusals one run tolerates before it stops
    failures_in_row: int = 3  # consecutive failed symbols that stop the run (a dead session)
    max_reject_pct: Decimal = Decimal("0.5")
    max_reject_run: int = 12
    write_batch: int = 25
    flush_bars: int = 3_000_000  # a batch is written early once it holds this many bars

    def __post_init__(self) -> None:
        values = (
            self.chunk_days,
            self.per_minute,
            self.per_day,
            self.plan_days,
            self.candle_cap,
            self.max_reject_run,
            self.write_batch,
            self.flush_bars,
        )
        if (
            min(values) < 1
            or self.max_clamps < 0
            or self.failures_in_row < 1
            or self.max_reject_pct < 0
        ):
            raise MinuteHistoryError("history_bad_limits")

    @property
    def candle_cap(self) -> int:
        return self.max_candles if self.max_candles is not None else 400 * self.chunk_days

    @classmethod
    def from_settings(cls, settings: HatSettings, **overrides: int | None) -> HistoryLimits:
        """The settings' limits (the day cap keeps the reserve for live trading); an override
        that is None keeps the setting."""
        values: dict[str, Any] = {
            "chunk_days": settings.history_chunk_days,
            "per_minute": settings.history_per_minute,
            "per_day": settings.history_calls_per_day,
            "plan_days": settings.history_plan_days,
            "max_reject_pct": settings.history_max_reject_pct,
            "max_reject_run": settings.history_max_reject_run,
            "write_batch": settings.history_write_batch,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


Sleep = Callable[[float], None]


@dataclass
class Progress:
    """What a download has done, kept across runs: history calls per IST day (the daily budget
    holds across restarts), the period each symbol was loaded for, the earliest date the plan
    serves once a call was refused for it, and the plan depth (`plan_days`) that date and the
    last run's period were learned under (a file from before it was recorded was written under
    the Standard plan's 365 days). `path` None keeps it in memory only."""

    path: Path | None = None
    calls: dict[str, int] = field(default_factory=dict)
    done: dict[str, tuple[date, date]] = field(default_factory=dict)
    available_from: date | None = None
    window: tuple[date, date] | None = None  # the last run's period, which a resume continues
    recent: list[datetime] = field(default_factory=list)  # the last minute's call instants
    failed: dict[str, str] = field(default_factory=dict)  # symbols whose last attempt failed
    plan_days: int = LEGACY_PLAN_DAYS

    @classmethod
    def load(cls, path: Path) -> Progress:
        """The progress stored at `path`; empty when there is none. An unreadable file raises
        `history_bad_progress`: guessing would lose the day's call count."""
        if not path.is_file():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            calls = {str(day): int(n) for day, n in raw.get("calls", {}).items()}
            done = {
                str(symbol): (date.fromisoformat(span[0]), date.fromisoformat(span[1]))
                for symbol, span in raw.get("done", {}).items()
            }
            floor = raw.get("available_from")
            window = raw.get("window")
            return cls(
                path,
                calls,
                done,
                None if floor is None else date.fromisoformat(floor),
                None
                if window is None
                else (date.fromisoformat(window[0]), date.fromisoformat(window[1])),
                [as_utc(datetime.fromisoformat(at)) for at in raw.get("recent", [])],
                {str(symbol): str(code) for symbol, code in raw.get("failed", {}).items()},
                int(raw.get("plan_days", LEGACY_PLAN_DAYS)),
            )
        except (OSError, ValueError, TypeError, AttributeError, IndexError) as exc:
            raise MinuteHistoryError("history_bad_progress") from exc

    def save(self) -> None:
        """Write the progress whole through a temporary file and an atomic replace."""
        if self.path is None:
            return
        body = {
            "calls": dict(sorted(self.calls.items())),
            "done": {s: [a.isoformat(), b.isoformat()] for s, (a, b) in sorted(self.done.items())},
            "available_from": None
            if self.available_from is None
            else self.available_from.isoformat(),
            "window": None if self.window is None else [d.isoformat() for d in self.window],
            "recent": [as_utc(at).isoformat() for at in self.recent],
            "failed": dict(sorted(self.failed.items())),
            "plan_days": self.plan_days,
        }
        text = json.dumps(body)
        atomic_replace(self.path, lambda tmp: tmp.write_text(text, encoding="utf-8"))

    def covers(self, symbol: str, start: date, end: date) -> bool:
        span = self.done.get(symbol)
        return span is not None and span[0] <= start and span[1] >= end


class CallPacer:
    """Makes each history call within the budget: at most `per_minute` calls in any 60 seconds
    (it sleeps until the oldest leaves the window) and at most `per_day` in an IST day (both
    kept in `progress`, so a restart shares the minute and the day; one more call than the day
    allows raises `history_daily_limit`). The progress is saved before every call: the count
    must survive a crash.
    A call counts when it is made, whatever it answers."""

    def __init__(
        self,
        limits: HistoryLimits,
        progress: Progress,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._limits = limits
        self._progress = progress
        self._clock = clock
        self._sleep = sleep
        # seeded from the progress: a run started right after another sees its last minute
        self._recent: deque[datetime] = deque(sorted(progress.recent))
        self.made = 0
        self.last_day: date | None = None  # the IST day of the last call made

    def today(self) -> date:
        return self._clock.now().astimezone(IST).date()

    def call(self, history: HistoryCall, request: dict[str, str]) -> object:
        now = self._clock.now()
        while True:
            while self._recent and self._recent[0] <= now - _WINDOW:
                self._recent.popleft()
            if len(self._recent) < self._limits.per_minute:
                break
            self._sleep((self._recent[0] + _WINDOW - now).total_seconds())
            now = self._clock.now()
        day = now.astimezone(IST).date().isoformat()  # after any wait: the day the call is made
        if self._progress.calls.get(day, 0) >= self._limits.per_day:
            raise MinuteHistoryError("history_daily_limit")
        self._recent.append(now)
        self._progress.recent = list(self._recent)
        self._progress.calls[day] = self._progress.calls.get(day, 0) + 1
        self.made += 1
        self.last_day = date.fromisoformat(day)
        self._progress.save()
        try:
            return history(request)
        except Exception as exc:  # the SDK's text never leaves this module
            raise MinuteHistoryError("history_failed") from exc


def _plan_refused(response: Mapping[str, object]) -> bool:
    """A Fyers -50 whose reason is the plan's history depth ("range_from exceeds the historical
    data available for your plan"), as opposed to any other -50 (a bad range, an IP rule)."""
    if response.get("code") != -50:
        return False
    data = response.get("data")
    texts = [response.get("message")]
    if isinstance(data, Mapping):
        texts.extend(data.values())
    return any(isinstance(t, str) and _PLAN_REFUSAL in t.lower() for t in texts)


def _request(symbol: str, first: date, last: date) -> dict[str, str]:
    return {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": first.isoformat(),
        "range_to": last.isoformat(),
        "cont_flag": "1",
    }


@dataclass
class HistoryReport:
    """What one run did; `days` is the trade dates stored for each loaded symbol, `rejects`
    the ledger rows it wrote by rule."""

    start: date
    end: date
    symbols: int = 0
    days: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    skipped_failed: int = 0  # failed on data before; --retry-failed asks for them again
    failed: dict[str, str] = field(default_factory=dict)
    rows: int = 0
    calls: int = 0
    available_from: date | None = None
    clamps: int = 0
    stopped: str | None = None
    rejects: Counter[str] = field(default_factory=Counter)
    writes: int = 0  # batches written

    def summary(self) -> str:
        """A short coverage report."""
        counts = sorted(self.days.values())
        with_bars = [n for n in counts if n]
        reached = len(counts) + self.skipped + self.skipped_failed + len(self.failed)
        lines = [
            f"minute history {self.start.isoformat()}..{self.end.isoformat()}"
            + (
                f" (plan serves from {self.available_from.isoformat()})"
                if self.available_from
                else ""
            ),
            f"symbols {self.symbols}: loaded {len(counts)} ({len(counts) - len(with_bars)} "
            f"without bars), skipped as done {self.skipped}, skipped as failed on data "
            f"{self.skipped_failed}, failed {len(self.failed)}, "
            f"not reached {self.symbols - reached}",
            f"rows written {self.rows} in {self.writes} batches, calls {self.calls}",
        ]
        if with_bars:
            lines.append(
                f"trade dates with bars per symbol: min {with_bars[0]}, "
                f"median {with_bars[len(with_bars) // 2]}, max {with_bars[-1]}"
            )
        if self.rejects:
            lines.append(
                "rejected (ledger bars_5m_rejects): "
                + ", ".join(f"{r} {n}" for r, n in sorted(self.rejects.items()))
            )
        if self.failed:
            codes = Counter(self.failed.values())
            lines.append("failed: " + ", ".join(f"{c} {n}" for c, n in sorted(codes.items())))
        if self.stopped:
            lines.append(f"stopped: {self.stopped} (run again with --resume)")
        return "\n".join(lines) + "\n"


@dataclass
class _Loaded:
    """One symbol's answers: its raw-basis bars, its rejected candles and how many candles
    came (kept or not)."""

    first: date
    bars: list[Bar] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    candles: int = 0
    longest_run: int = 0

    def too_many_rejects(self, limits: HistoryLimits) -> bool:
        return self.longest_run >= limits.max_reject_run or Decimal(
            len(self.rejected)
        ) * 100 > limits.max_reject_pct * max(self.candles, 1)


@dataclass
class _Batch:
    """Symbols loaded but not yet written, and the ledger rows waiting with them."""

    loaded: list[tuple[str, str, date, date, list[Bar]]] = field(default_factory=list)
    rejects: list[RejectRow] = field(default_factory=list)
    bars: int = 0

    def full(self, limits: HistoryLimits) -> bool:
        return len(self.loaded) >= limits.write_batch or self.bars >= limits.flush_bars


def _load_symbol(
    history: HistoryCall,
    pacer: CallPacer,
    limits: HistoryLimits,
    progress: Progress,
    report: HistoryReport,
    basis: RawBasis,
    instrument_id: str,
    symbol: str,
    start: date,
    end: date,
) -> _Loaded:
    """The raw-basis 5-minute bars of `symbol` for `start..end` (or from where the plan serves,
    when a call is refused for its depth). Calls span at most `chunk_days`; each response is
    screened, aggregated and converted with the factors as of the day it was fetched as it
    arrives, so only one response's candles are held."""
    loaded = _Loaded(start)
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=limits.chunk_days - 1))
        response = pacer.call(history, _request(symbol, first, last))
        if not isinstance(response, Mapping):
            raise MinuteHistoryError("history_bad_answer")  # not a refusal: the symbol only
        if response.get("s") != "ok" and response.get("code") == FYERS_CODE_RATE_LIMITED:
            raise MinuteHistoryError("history_rate_limited")
        if response.get("s") != "ok" and _plan_refused(response):
            first = loaded.first = _clamp(pacer, limits, progress, report, first)
            continue
        parsed = _parse_answer(response, limits, first, last)
        fetched_on = pacer.last_day or pacer.today()
        loaded.candles += len(parsed.candles) + len(parsed.rejected)
        loaded.rejected.extend(parsed.rejected)
        loaded.longest_run = max(loaded.longest_run, parsed.longest_run)
        loaded.bars.extend(
            to_raw(bar, basis.factor(instrument_id, trade_date_of(bar.start), fetched_on))
            for bar in aggregate(symbol, screen(parsed))
        )
        first = last + timedelta(days=1)
    return loaded


def _clamp(
    pacer: CallPacer, limits: HistoryLimits, progress: Progress, report: HistoryReport, first: date
) -> date:
    """The next first date after a refusal for the plan's depth: where the plan serves, or a
    day later when that was refused too; `history_plan_limit` after `max_clamps` refusals."""
    if report.clamps >= limits.max_clamps:
        raise MinuteHistoryError("history_plan_limit")
    report.clamps += 1
    first = max(first + timedelta(days=1), pacer.today() - timedelta(days=limits.plan_days))
    if progress.available_from is None or first > progress.available_from:
        progress.available_from = first
        progress.save()
    report.available_from = progress.available_from
    return first


def _parse_answer(
    response: Mapping[str, object], limits: HistoryLimits, first: date, last: date
) -> Parsed:
    """The candles and rejected rows of one answer for `first..last`; a refusal or an answer
    that is not a candle list raises."""
    status = response.get("s")
    if status not in ("ok", "no_data"):
        raise MinuteHistoryError("history_failed")
    raw = response.get("candles")
    if raw is None and status == "no_data":
        raw = []
    if not isinstance(raw, list):  # an "ok" without candles is not an empty range
        raise MinuteHistoryError("history_bad_answer")
    if len(raw) > limits.candle_cap:
        raise MinuteHistoryError("history_too_many_candles")
    parsed = parse_candles(raw)

    # a rejected row counts wherever it claims to be: an answer is judged whole
    inside = [c for c in parsed.candles if first <= trade_date_of(c.at) <= last]
    return Parsed(inside, parsed.rejected, parsed.longest_run)


def load_minute_history(
    history: HistoryCall,
    store: BarStore,
    instruments: Mapping[str, uuid.UUID | str],
    start: date,
    end: date,
    *,
    basis: RawBasis,
    limits: HistoryLimits | None = None,
    progress: Progress | None = None,
    pacer: CallPacer | None = None,
    resume: bool = False,
    retry_failed: bool = False,
) -> HistoryReport:
    """Fetch the 1-minute history of each broker symbol in `instruments` (broker symbol ->
    instrument id) for `start..end`, aggregate it, convert it to the raw basis and write the
    days that fit the raw daily bar into `bars_5m`, `write_batch` symbols at a time (a batch is
    written, and its symbols marked done, before the next starts; D131: a later failure keeps
    what was written). Every refused candle and day goes to the reject ledger; a symbol fails
    only above the reject threshold.

    With `resume`, the period is the one `progress` recorded for the last run (a later day would
    otherwise ask for a shifted one; a different length is refused) and a symbol it records as
    loaded over it is skipped, as is one that failed on its data (`_DATA_FAILURES`) unless
    `retry_failed`. A symbol that fails is reported and the run goes on; `failures_in_row`
    transport failures in a row, the day's call budget or too many plan-limit refusals stop it,
    with the progress saved."""
    limits = limits or HistoryLimits()
    progress = progress if progress is not None else Progress()
    start, end = _begin(progress, start, end, resume, limits.plan_days)
    pacer = pacer or CallPacer(limits, progress)
    report = HistoryReport(start, end, symbols=len(instruments))
    report.available_from = progress.available_from
    batch = _Batch()
    in_row = 0
    # symbols that failed last time go last, so a few bad ones cannot block a resume forever
    for symbol in sorted(instruments, key=lambda s: (s in progress.failed, s)):
        first = max(start, progress.available_from or start)
        if resume and _skipped(progress, report, symbol, first, end, retry_failed):
            continue
        code = (
            "history_plan_limit"  # the plan serves nothing of the period
            if first > end
            else _attempt(
                history,
                batch,
                str(instruments[symbol]),
                pacer,
                limits,
                progress,
                report,
                basis,
                symbol,
                first,
                end,
            )
        )
        # only refused calls in a row point at a dead session; a symbol's bad data does not
        in_row = in_row + 1 if code == "history_failed" else 0
        report.stopped = _stop(code, in_row, limits)
        if report.stopped:
            break
        if batch.full(limits):
            _flush(store, basis, batch, progress, report)
    _flush(store, basis, batch, progress, report)
    return report


def _skipped(
    progress: Progress, report: HistoryReport, symbol: str, first: date, end: date, retry: bool
) -> bool:
    """Whether a resume skips `symbol` (counted in the report): loaded over the period, or
    failed on its data and not asked to `retry`."""
    if first <= end and progress.covers(symbol, first, end):
        report.skipped += 1
        return True
    if not retry and progress.failed.get(symbol) in _DATA_FAILURES:
        report.skipped_failed += 1
        return True
    return False


def _begin(
    progress: Progress, start: date, end: date, resume: bool, plan_days: int
) -> tuple[date, date]:
    """The run's period (a resume continues the stopped run's), recorded in the progress; a
    fresh run forgets the symbols done. A changed plan depth forgets where the old plan served
    and the old period (they were learned under another plan); a resume asked for a period of
    another length than the stored one is refused, never silently replaced."""
    if start > end:
        raise MinuteHistoryError("history_bad_period")
    if progress.plan_days != plan_days:
        progress.plan_days = plan_days
        progress.available_from = None
        progress.window = None
    if resume and progress.window is not None:
        if progress.window[1] - progress.window[0] != end - start:
            raise MinuteHistoryError("history_bad_period")
        start, end = progress.window
    if not resume:
        progress.done.clear()
    progress.window = (start, end)
    progress.save()
    return start, end


def _stop(code: str | None, in_row: int, limits: HistoryLimits) -> str | None:
    """Why the run stops after a symbol's outcome `code`, or None to go on."""
    if code in _RUN_STOPS:
        return code
    return "history_failed" if in_row >= limits.failures_in_row else None


def _attempt(
    history: HistoryCall,
    batch: _Batch,
    instrument_id: str,
    pacer: CallPacer,
    limits: HistoryLimits,
    progress: Progress,
    report: HistoryReport,
    basis: RawBasis,
    symbol: str,
    first: date,
    end: date,
) -> str | None:
    """Load one symbol into `batch`; None when it loaded, else the code it failed with (a symbol
    failure is recorded, a run stop is left to the caller). Its rejected candles go to the
    ledger either way."""
    try:
        loaded = _load_symbol(
            history, pacer, limits, progress, report, basis, instrument_id, symbol, first, end
        )
    except MinuteHistoryError as exc:
        if exc.code not in _RUN_STOPS:
            report.failed[symbol] = progress.failed[symbol] = exc.code
            progress.save()
        return exc.code
    finally:
        report.calls = pacer.made
    rules = BarBuilder()
    for r in loaded.rejected:
        bucket = rules.bar_start(r.at) if r.at is not None else None
        day = trade_date_of(r.at) if r.at is not None else None
        batch.rejects.append(RejectRow(instrument_id, symbol, day, bucket, r.rule))
        report.rejects[r.rule] += 1
    if loaded.too_many_rejects(limits):
        report.failed[symbol] = progress.failed[symbol] = "history_too_many_rejects"
        progress.save()
        return "history_too_many_rejects"
    batch.loaded.append((symbol, instrument_id, loaded.first, end, loaded.bars))
    batch.bars += len(loaded.bars)
    return None


def _checked_days(
    bars: Sequence[Bar],
    instrument_id: str,
    daily: Mapping[tuple[str, date], DailyRange],
    tolerance: Decimal,
) -> tuple[list[Bar], list[tuple[date, str]]]:
    """The bars of the days that fit the raw daily bar, and the refused days with their code."""
    by_day: dict[date, list[Bar]] = {}
    for bar in sorted(bars, key=lambda b: b.start):
        by_day.setdefault(trade_date_of(bar.start), []).append(bar)
    kept: list[Bar] = []
    refused: list[tuple[date, str]] = []
    for day, group in sorted(by_day.items()):
        code = check_day(group, daily.get((instrument_id, day)), tolerance)
        if code is None:
            kept.extend(group)
        else:
            refused.append((day, code))
    return kept, refused


def _flush(
    store: BarStore, basis: RawBasis, batch: _Batch, progress: Progress, report: HistoryReport
) -> None:
    """Check the batch's days against the raw daily bars, write the ones that fit in one write
    (each symbol's stored days over its period are replaced, so a refused day keeps no older
    bar), append the ledger and only then mark the symbols done."""
    if batch.loaded:
        ids = sorted({instrument_id for _, instrument_id, _, _, _ in batch.loaded})
        first = min(f for _, _, f, _, _ in batch.loaded)
        last = max(e for _, _, _, e, _ in batch.loaded)
        daily = basis.daily(ids, first, last)
        keep: list[Bar] = []
        for symbol, instrument_id, _, _, bars in batch.loaded:
            kept, refused = _checked_days(bars, instrument_id, daily, basis.tolerance)
            keep.extend(kept)
            report.days[symbol] = len({trade_date_of(b.start) for b in kept})
            for day, code in refused:
                batch.rejects.append(RejectRow(instrument_id, symbol, day, None, code))
                report.rejects[code] += 1
        report.rows += store.write(
            keep,
            {symbol: instrument_id for symbol, instrument_id, _, _, _ in batch.loaded},
            replace={instrument_id: (f, e) for _, instrument_id, f, e, _ in batch.loaded},
        )
        report.writes += 1
    store.write_rejects(batch.rejects)
    for symbol, _, first, end, _ in batch.loaded:
        progress.done[symbol] = (first, end)
        progress.failed.pop(symbol, None)
    progress.save()
    batch.loaded.clear()
    batch.rejects.clear()
    batch.bars = 0


def rebase_stored(
    source: BarStore, target: BarStore, basis: RawBasis, fetched_on: date, first: date, last: date
) -> HistoryReport:
    """Re-derive the raw basis of bars already stored by an earlier download, fetched on
    `fetched_on` over `first..last`, into `target` (another root: a converted day must never be
    converted twice), with the envelope check and the ledger of a download; no Fyers call. One
    write per day; `days` counts the stored days per instrument."""
    report = HistoryReport(first, last)
    for day in source.days():
        if not first <= day <= last:
            continue
        by_instrument: dict[str, list[Bar]] = {}
        for row in source.read(day):
            factor = basis.factor(row.instrument_id, day, fetched_on)
            bar = Bar(
                row.instrument_id,
                row.bar_start.astimezone(IST),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
            )
            by_instrument.setdefault(row.instrument_id, []).append(to_raw(bar, factor))
        daily = basis.daily(sorted(by_instrument), day, day)
        keep: list[Bar] = []
        rejects: list[RejectRow] = []
        for instrument_id, bars in by_instrument.items():
            kept, refused = _checked_days(bars, instrument_id, daily, basis.tolerance)
            keep.extend(kept)
            if kept:
                report.days[instrument_id] = report.days.get(instrument_id, 0) + 1
            for refused_day, code in refused:
                rejects.append(RejectRow(instrument_id, instrument_id, refused_day, None, code))
                report.rejects[code] += 1
        report.rows += target.write(keep, {i: i for i in by_instrument})
        report.writes += 1
        target.write_rejects(rejects)
    report.symbols = len(report.days)
    return report


# --- the trading universe and the command line ----------------------------------------------


def history_universe(session: Session) -> tuple[dict[str, uuid.UUID], int]:
    """The active NSE EQ/BE instruments (the trading universe) by their Fyers symbol, and
    how many active members have no Fyers symbol (they cannot be asked for)."""
    rows = session.execute(
        select(HatInstrument.id, HatBrokerSymbol.broker_symbol)
        .join(HatMarket, HatMarket.id == HatInstrument.market_id)
        .outerjoin(
            HatBrokerSymbol,
            (HatBrokerSymbol.instrument_id == HatInstrument.id)
            & (HatBrokerSymbol.broker == BROKER_FYERS),
        )
        .where(
            HatMarket.code == "NSE",
            HatInstrument.active.is_(True),
            HatInstrument.series.in_(UNIVERSE_SERIES),
        )
    ).all()
    mapped = {symbol: instrument_id for instrument_id, symbol in rows if symbol is not None}
    return mapped, sum(1 for _, symbol in rows if symbol is None)


@contextmanager
def _run_lock(path: Path) -> Iterator[None]:
    """One download at a time per data root: a second run would share the progress file, the
    call budget and the day partitions. The OS lock goes with the process, so a crash leaves
    nothing to clean up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MinuteHistoryError("history_already_running") from exc
        yield


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.minute_history", description=__doc__)
    p.add_argument("--days", type=int, help="days back from today (default: the plan's depth)")
    p.add_argument(
        "--resume",
        action="store_true",
        help="continue the last run's period, skipping symbols done",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="with --resume, ask again for symbols that failed on their data",
    )
    p.add_argument("--chunk-days", type=int, help="override HAT_HISTORY_CHUNK_DAYS")
    p.add_argument("--per-minute", type=int, help="override HAT_HISTORY_PER_MINUTE")
    p.add_argument("--per-day", type=int, help="override the day cap (limit less reserve)")
    p.add_argument("--plan-days", type=int, help="override HAT_HISTORY_PLAN_DAYS")
    p.add_argument(
        "--rebase-into",
        type=Path,
        help="no download: convert the stored bars_5m of the last run's period to the raw basis "
        "into this other data root (needs --fetched-on)",
    )
    p.add_argument(
        "--fetched-on",
        type=date.fromisoformat,
        help="with --rebase-into, the IST day the stored bars were downloaded",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real Fyers and DB
    """python -m app.hat.data.minute_history [--days N] [--resume [--retry-failed]]
    python -m app.hat.data.minute_history --rebase-into ROOT --fetched-on YYYY-MM-DD

    Loads the trading universe's 1-minute history through HAT's Fyers session (log in from the
    app first) into `bars_5m` under `HAT_DATA_ROOT`, from `--days` back to yesterday (today's
    bars belong to the live builder). Progress is kept in `minute_history/progress.json`, so a
    run stopped by the day's call budget or a failure continues with `--resume`. Run it outside
    market hours: the day cap keeps `HAT_HISTORY_RESERVE` calls for live trading, but the per-
    minute limit is shared with a running server too."""
    args = _parse_args(argv)
    try:
        return _run(args)
    except MinuteHistoryError as exc:
        sys.stderr.write(f"{exc.code}\n")
        return 2


def _run(args: argparse.Namespace) -> int:  # pragma: no cover - real Fyers and DB
    from ..settings import operator_settings
    from .provider.fyers_websocket import fyers_history

    settings = operator_settings()
    limits = HistoryLimits.from_settings(
        settings,
        chunk_days=args.chunk_days,
        per_minute=args.per_minute,
        per_day=args.per_day,
        plan_days=args.plan_days,
    )
    days = args.days if args.days is not None else limits.plan_days
    if days < 1:
        raise MinuteHistoryError("history_bad_period")
    logging.basicConfig(level=logging.INFO)
    root = settings.data_root
    with _run_lock(root / PROGRESS_FILE.with_suffix(".lock")):
        basis = _basis(settings)
        if args.rebase_into is not None:
            return _rebase(args, root, basis)
        return _download(args, days, limits, root, basis, fyers_history)


def _basis(settings: HatSettings) -> RawBasis:  # pragma: no cover - real DB
    """The stored corporate actions and the raw daily bars of the data root."""
    from app.database import db_session_scope

    from .corporate_actions import stored_factors

    with db_session_scope() as db:
        actions = stored_factors(db)
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    return RawBasis(actions, store_daily(store), settings.history_envelope_tol)


def _rebase(
    args: argparse.Namespace, root: Path, basis: RawBasis
) -> int:  # pragma: no cover - real data
    progress = Progress.load(root / PROGRESS_FILE)
    target = Path(args.rebase_into)
    if args.fetched_on is None or progress.window is None:
        raise MinuteHistoryError("history_bad_period")
    if target.resolve() == root.resolve():
        raise MinuteHistoryError("history_rebase_in_place")  # a day must not convert twice
    first, last = progress.window
    report = rebase_stored(BarStore(root), BarStore(target), basis, args.fetched_on, first, last)
    sys.stdout.write(report.summary())
    return 0


def _download(
    args: argparse.Namespace,
    days: int,
    limits: HistoryLimits,
    root: Path,
    basis: RawBasis,
    fyers_history: Callable[[], HistoryCall | None],
) -> int:  # pragma: no cover - real Fyers and DB
    from app.database import db_session_scope

    progress = Progress.load(root / PROGRESS_FILE)
    history = fyers_history()
    if history is None:
        sys.stderr.write("history_no_session: log in to Fyers from the app first\n")
        return 2
    with db_session_scope() as db:
        instruments, unmapped = history_universe(db)
    today = datetime.now(IST).date()
    report = load_minute_history(
        history,
        BarStore(root),
        instruments,
        today - timedelta(days=days),
        today - timedelta(days=1),
        basis=basis,
        limits=limits,
        progress=progress,
        resume=args.resume,
        retry_failed=args.retry_failed,
    )
    text = report.summary() + f"active members without a Fyers symbol: {unmapped}\n"
    sys.stdout.write(text)
    save_report(root / PROGRESS_FILE.parent, text, datetime.now(IST))
    return 1 if report.stopped or report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
