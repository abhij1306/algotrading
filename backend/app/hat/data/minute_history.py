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

The download (owner, D131) runs over the Fyers Standard plan: 1-minute candles for the last 365
days only, at most 100 days a call, 50 calls a minute and 5,000 a day (`HistoryLimits`, all
configurable). Each symbol is written as soon as it loads; `Progress` keeps the day's call count
and the symbols done, so `--resume` continues a stopped run. A -50 refusal for the plan's depth
clamps the start date to where the plan serves; it is not a failure. Older intraday history is to
come from NSE later (owner pending).

The SDK call is injected (`HistoryCall`, the shape of `fyersModel.FyersModel.history`): only
`main` builds it, from HAT's own Fyers session through the data provider's `fyers_history`, and
tests never use a real client. A response that is not usable raises `MinuteHistoryError` with a
fixed code, never the broker's text.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatBrokerSymbol, HatInstrument, HatMarket
from ..models.strategy import BROKER_FYERS
from ..time import as_utc, trade_date_of
from .bars import Bar, BarBuilder, Tick
from .bars_store import BarStore
from .ingest import UNIVERSE_SERIES
from .run_report import save_report
from .sessions import IST
from .store import atomic_replace

__all__ = [
    "CHUNK_DAYS",
    "PROGRESS_FILE",
    "CallPacer",
    "HistoryCall",
    "HistoryLimits",
    "HistoryReport",
    "MinuteCandle",
    "MinuteHistoryError",
    "Progress",
    "aggregate",
    "candle_prints",
    "history_universe",
    "load_minute_history",
    "main",
    "parse_candles",
]

CHUNK_DAYS = 100  # the most days of 1-minute candles one history request may span
_MINUTE = timedelta(minutes=1)
_PLACES = Decimal("0.0001")
_WINDOW = timedelta(minutes=1)  # the per-minute call budget's window
_PLAN_REFUSAL = "historical data available for your plan"
# Fyers' answer once the app has spent its plan's daily requests ("Standard plan limit of 5,000
# requests/day"): every call fails until the next day, so it ends the run (HAT-BRK-082).
_FYERS_RATE_LIMITED = -360
# Codes that end the run (the rest fail one symbol): the day's call budget is spent (by our count
# or by Fyers'), or the plan keeps refusing the period.
_RUN_STOPS = frozenset({"history_daily_limit", "history_rate_limited", "history_plan_limit"})
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


def _price(raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise MinuteHistoryError("history_bad_candle")
    try:
        value = Decimal(repr(raw)).quantize(_PLACES)
    except InvalidOperation as exc:  # too large for the decimal context
        raise MinuteHistoryError("history_bad_candle") from exc
    if value <= 0:
        raise MinuteHistoryError("history_bad_candle")
    return value


def _epoch(raw: object) -> datetime:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MinuteHistoryError("history_bad_candle")
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise MinuteHistoryError("history_bad_candle") from exc


def _volume(raw: object) -> int:
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise MinuteHistoryError("history_bad_candle")
    return raw


def parse_candles(raw: Iterable[Sequence[object]]) -> list[MinuteCandle]:
    """Candles from the history response's `candles` ([epoch seconds, open, high, low, close,
    volume] each), oldest first. A malformed or inconsistent candle raises; nothing is guessed."""
    found: dict[datetime, MinuteCandle] = {}
    for row in raw:
        if not isinstance(row, list | tuple) or len(row) != 6:
            raise MinuteHistoryError("history_bad_candle")
        at = _epoch(row[0])
        volume = _volume(row[5])
        open_, high, low, close = (_price(v) for v in row[1:5])
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise MinuteHistoryError("history_bad_candle")
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


# --- the download (D131: the Fyers Standard plan serves 1-minute candles for the last 365 days) --


@dataclass(frozen=True)
class HistoryLimits:
    """The Fyers Standard plan's history limits (owner, D131), configurable from the command
    line: days per call, calls per minute and per IST day, how far back the plan serves intraday
    candles, and the most candles one response may hold (by default 400 a day of the call, over
    the session's 375 minutes; a larger answer is refused, so one symbol never holds more than
    one response of candles at a time)."""

    chunk_days: int = CHUNK_DAYS
    per_minute: int = 50
    per_day: int = 5_000
    plan_days: int = 365
    max_candles: int | None = None  # None: 400 x chunk_days
    max_clamps: int = 3  # plan-limit refusals one run tolerates before it stops
    failures_in_row: int = 3  # consecutive failed symbols that stop the run (a dead session)

    def __post_init__(self) -> None:
        values = (self.chunk_days, self.per_minute, self.per_day, self.plan_days, self.candle_cap)
        if min(values) < 1 or self.max_clamps < 0 or self.failures_in_row < 1:
            raise MinuteHistoryError("history_bad_limits")

    @property
    def candle_cap(self) -> int:
        return self.max_candles if self.max_candles is not None else 400 * self.chunk_days


Clock = Callable[[], datetime]
Sleep = Callable[[float], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Progress:
    """What a download has done, kept across runs: history calls per IST day (the daily budget
    holds across restarts), the period each symbol was loaded for, and the earliest date the plan
    serves once a call was refused for it. `path` None keeps it in memory only."""

    path: Path | None = None
    calls: dict[str, int] = field(default_factory=dict)
    done: dict[str, tuple[date, date]] = field(default_factory=dict)
    available_from: date | None = None
    window: tuple[date, date] | None = None  # the last run's period, which a resume continues
    recent: list[datetime] = field(default_factory=list)  # the last minute's call instants
    failed: dict[str, str] = field(default_factory=dict)  # symbols whose last attempt failed

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
        clock: Clock = _utc_now,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._limits = limits
        self._progress = progress
        self._clock = clock
        self._sleep = sleep
        # seeded from the progress: a run started right after another sees its last minute
        self._recent: deque[datetime] = deque(sorted(progress.recent))
        self.made = 0

    def today(self) -> date:
        return self._clock().astimezone(IST).date()

    def call(self, history: HistoryCall, request: dict[str, str]) -> object:
        now = self._clock()
        while True:
            while self._recent and self._recent[0] <= now - _WINDOW:
                self._recent.popleft()
            if len(self._recent) < self._limits.per_minute:
                break
            self._sleep((self._recent[0] + _WINDOW - now).total_seconds())
            now = self._clock()
        day = now.astimezone(IST).date().isoformat()  # after any wait: the day the call is made
        if self._progress.calls.get(day, 0) >= self._limits.per_day:
            raise MinuteHistoryError("history_daily_limit")
        self._recent.append(now)
        self._progress.recent = list(self._recent)
        self._progress.calls[day] = self._progress.calls.get(day, 0) + 1
        self.made += 1
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
    """What one run did; `days` is the trade dates with bars of each loaded symbol."""

    start: date
    end: date
    symbols: int = 0
    days: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    rows: int = 0
    calls: int = 0
    available_from: date | None = None
    clamps: int = 0
    stopped: str | None = None

    def summary(self) -> str:
        """A short coverage report."""
        counts = sorted(self.days.values())
        with_bars = [n for n in counts if n]
        lines = [
            f"minute history {self.start.isoformat()}..{self.end.isoformat()}"
            + (
                f" (plan serves from {self.available_from.isoformat()})"
                if self.available_from
                else ""
            ),
            f"symbols {self.symbols}: loaded {len(counts)} ({len(counts) - len(with_bars)} "
            f"without candles), skipped as done {self.skipped}, failed {len(self.failed)}, "
            f"not reached {self.symbols - len(counts) - self.skipped - len(self.failed)}",
            f"rows written {self.rows}, calls {self.calls}",
        ]
        if with_bars:
            lines.append(
                f"trade dates with bars per symbol: min {with_bars[0]}, "
                f"median {with_bars[len(with_bars) // 2]}, max {with_bars[-1]}"
            )
        if self.failed:
            codes: dict[str, int] = {}
            for code in self.failed.values():
                codes[code] = codes.get(code, 0) + 1
            lines.append("failed: " + ", ".join(f"{c} {n}" for c, n in sorted(codes.items())))
        if self.stopped:
            lines.append(f"stopped: {self.stopped} (run again with --resume)")
        return "\n".join(lines) + "\n"


def _load_symbol(
    history: HistoryCall,
    pacer: CallPacer,
    limits: HistoryLimits,
    progress: Progress,
    report: HistoryReport,
    symbol: str,
    start: date,
    end: date,
) -> tuple[date, list[Bar]]:
    """The 5-minute bars of `symbol` for `start..end` (or from where the plan serves, when a call
    is refused for its depth) and the first date asked for. Calls span at most `chunk_days`;
    each response is aggregated as it arrives, so only one response's candles are held."""
    bars: list[Bar] = []
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=limits.chunk_days - 1))
        response = pacer.call(history, _request(symbol, first, last))
        if not isinstance(response, Mapping):
            raise MinuteHistoryError("history_bad_answer")  # not a refusal: the symbol only
        if response.get("s") != "ok" and response.get("code") == _FYERS_RATE_LIMITED:
            raise MinuteHistoryError("history_rate_limited")
        if response.get("s") != "ok" and _plan_refused(response):
            first = start = _clamp(pacer, limits, progress, report, first)
            continue
        bars.extend(_bars_of(symbol, response, limits, first, last))
        first = last + timedelta(days=1)
    return start, bars


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


def _bars_of(
    symbol: str, response: Mapping[str, object], limits: HistoryLimits, first: date, last: date
) -> list[Bar]:
    """The 5-minute bars of one answer for `first..last`; a refusal or a bad answer raises."""
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
    candles = [c for c in parse_candles(raw) if first <= trade_date_of(c.at) <= last]
    return aggregate(symbol, candles)


def load_minute_history(
    history: HistoryCall,
    store: BarStore,
    instruments: Mapping[str, uuid.UUID | str],
    start: date,
    end: date,
    *,
    limits: HistoryLimits | None = None,
    progress: Progress | None = None,
    pacer: CallPacer | None = None,
    resume: bool = False,
) -> HistoryReport:
    """Fetch the 1-minute history of each broker symbol in `instruments` (broker symbol ->
    instrument id) for `start..end`, aggregate it and merge it into `bars_5m`, writing each
    symbol as soon as it has loaded (D131: a later failure keeps what was written). With
    `resume`, the period is the one `progress` recorded for the last run (a later day would
    otherwise ask for a shifted one) and a symbol it records as loaded over it is skipped. A
    symbol that fails is reported and the run goes on; `failures_in_row` failed symbols in a
    row, the day's call budget or too many plan-limit refusals stop it, with the progress saved.
    Safe to re-run: `BarStore.write` keeps a bar that did not change."""
    progress = progress if progress is not None else Progress()
    start, end = _begin(progress, start, end, resume)
    limits = limits or HistoryLimits()
    pacer = pacer or CallPacer(limits, progress)
    report = HistoryReport(start, end, symbols=len(instruments))
    report.available_from = progress.available_from
    in_row = 0
    # symbols that failed last time go last, so a few bad ones cannot block a resume forever
    for symbol in sorted(instruments, key=lambda s: (s in progress.failed, s)):
        first = max(start, progress.available_from or start)
        if resume and first <= end and progress.covers(symbol, first, end):
            report.skipped += 1
            continue
        code = (
            "history_plan_limit"  # the plan serves nothing of the period
            if first > end
            else _attempt(
                history,
                store,
                instruments[symbol],
                pacer,
                limits,
                progress,
                report,
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
    return report


def _begin(progress: Progress, start: date, end: date, resume: bool) -> tuple[date, date]:
    """The run's period (a resume continues the stopped run's), recorded in the progress; a
    fresh run forgets the symbols done."""
    if resume and progress.window is not None:
        start, end = progress.window
    if start > end:
        raise MinuteHistoryError("history_bad_period")
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
    store: BarStore,
    instrument_id: uuid.UUID | str,
    pacer: CallPacer,
    limits: HistoryLimits,
    progress: Progress,
    report: HistoryReport,
    symbol: str,
    first: date,
    end: date,
) -> str | None:
    """Load and write one symbol; None when it loaded, else the code it failed with (a symbol
    failure is recorded, a run stop is left to the caller)."""
    try:
        first, bars = _load_symbol(history, pacer, limits, progress, report, symbol, first, end)
    except MinuteHistoryError as exc:
        if exc.code not in _RUN_STOPS:
            report.failed[symbol] = progress.failed[symbol] = exc.code
            progress.save()
        return exc.code
    finally:
        report.calls = pacer.made
    report.rows += store.write(bars, {symbol: instrument_id})
    report.days[symbol] = len({trade_date_of(b.start) for b in bars})
    progress.done[symbol] = (first, end)
    progress.failed.pop(symbol, None)
    progress.save()
    return None


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
    defaults = HistoryLimits()
    p.add_argument("--days", type=int, default=defaults.plan_days, help="days back from today")
    p.add_argument(
        "--resume",
        action="store_true",
        help="continue the last run's period, skipping symbols done",
    )
    p.add_argument("--chunk-days", type=int, default=defaults.chunk_days)
    p.add_argument("--per-minute", type=int, default=defaults.per_minute)
    p.add_argument("--per-day", type=int, default=defaults.per_day)
    p.add_argument("--plan-days", type=int, default=defaults.plan_days)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real Fyers and DB
    """python -m app.hat.data.minute_history --days 365 [--resume]

    Loads the trading universe's 1-minute history through HAT's Fyers session (log in from the
    app first) into `bars_5m` under `HAT_DATA_ROOT`, from `--days` back to yesterday (today's
    bars belong to the live builder). Progress is kept in `minute_history/progress.json`, so a
    run stopped by the day's call budget or a failure continues with `--resume`."""
    args = _parse_args(argv)
    try:
        return _run(args)
    except MinuteHistoryError as exc:
        sys.stderr.write(f"{exc.code}\n")
        return 2


def _run(args: argparse.Namespace) -> int:  # pragma: no cover - real Fyers and DB
    if args.days < 1:
        raise MinuteHistoryError("history_bad_period")
    limits = HistoryLimits(
        chunk_days=args.chunk_days,
        per_minute=args.per_minute,
        per_day=args.per_day,
        plan_days=args.plan_days,
    )
    from ..settings import operator_settings
    from .provider.fyers_websocket import fyers_history

    logging.basicConfig(level=logging.INFO)
    root = operator_settings().data_root
    with _run_lock(root / PROGRESS_FILE.with_suffix(".lock")):
        return _download(args, limits, root, fyers_history)


def _download(
    args: argparse.Namespace,
    limits: HistoryLimits,
    root: Path,
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
        today - timedelta(days=args.days),
        today - timedelta(days=1),
        limits=limits,
        progress=progress,
        resume=args.resume,
    )
    text = report.summary() + f"active members without a Fyers symbol: {unmapped}\n"
    sys.stdout.write(text)
    save_report(root / PROGRESS_FILE.parent, text, datetime.now(IST))
    return 1 if report.stopped or report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
