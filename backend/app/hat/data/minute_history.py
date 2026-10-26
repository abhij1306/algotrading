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

A bad candle (`candles.REJECT_RULES`, one code each) is rejected with its whole 5-minute bucket and
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
import logging
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ..broker_limits import FYERS_CODE_RATE_LIMITED
from ..clock import SYSTEM_CLOCK, Clock
from ..time import IST, trade_date_of
from .bars import Bar, BarBuilder
from .bars_basis import DailyRange, RawBasis, check_day, store_daily, to_raw
from .bars_store import BarStore, RejectRow
from .candles import Parsed, Rejected, aggregate, parse_candles, screen
from .minute_limits import HistoryLimits, MinuteHistoryError, Progress
from .pacing import DayLimitError, Pacer, Sleep
from .run_report import save_report, stop_reason
from .store import DataStore

if TYPE_CHECKING:
    from ..settings import HatSettings

__all__ = [
    "PROGRESS_FILE",
    "HistoryCall",
    "HistoryReport",
    "history_pacer",
    "load_minute_history",
    "main",
    "run_lock",
]

_PLAN_REFUSAL = "historical data available for your plan"
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


# --- the download (D131: the Fyers Standard plan serves 1-minute candles for the last 365 days) --


def history_pacer(
    limits: HistoryLimits,
    progress: Progress,
    *,
    clock: Clock = SYSTEM_CLOCK,
    sleep: Sleep = time.sleep,
) -> Pacer:
    """The history calls' pacer: at most `per_minute` calls in any 60 seconds and `per_day` in
    an IST day, both kept in `progress` (saved before every call: the count must survive a
    crash), so a restart shares the minute and the day."""
    return Pacer(
        per_minute=limits.per_minute,
        per_day=limits.per_day,
        state=progress,
        clock=clock,
        sleep=sleep,
    )


def _call(pacer: Pacer, history: HistoryCall, request: dict[str, str]) -> object:
    """One history call within the budget; one more than the day allows raises
    `history_daily_limit`. A call counts when it is made, whatever it answers."""
    try:
        pacer.wait()
    except DayLimitError as exc:
        raise MinuteHistoryError("history_daily_limit") from exc
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
    run: int = 0  # the rejected rows the last answer ended with

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


@dataclass
class Download:
    """One run's moving parts, built once by `load_minute_history`: the SDK call, the store,
    the pacer, the limits, the progress, the report, the raw basis and the batch waiting to be
    written."""

    history: HistoryCall
    store: BarStore
    pacer: Pacer
    limits: HistoryLimits
    progress: Progress
    report: HistoryReport
    basis: RawBasis
    batch: _Batch = field(default_factory=_Batch)


def _load_symbol(dl: Download, instrument_id: str, symbol: str, start: date, end: date) -> _Loaded:
    """The raw-basis 5-minute bars of `symbol` for `start..end` (or from where the plan serves,
    when a call is refused for its depth). Calls span at most `chunk_days`; each response is
    screened, aggregated and converted with the factors as of the day it was fetched as it
    arrives, so only one response's candles are held."""
    loaded = _Loaded(start)
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=dl.limits.chunk_days - 1))
        response = _call(dl.pacer, dl.history, _request(symbol, first, last))
        if not isinstance(response, Mapping):
            raise MinuteHistoryError("history_bad_answer")  # not a refusal: the symbol only
        if response.get("s") != "ok" and response.get("code") == FYERS_CODE_RATE_LIMITED:
            raise MinuteHistoryError("history_rate_limited")
        if response.get("s") != "ok" and _plan_refused(response):
            first = loaded.first = _clamp(dl, first)
            continue
        parsed = _parse_answer(response, dl.limits, first, last, loaded.run)
        loaded.run = parsed.trailing_run
        fetched_on = dl.pacer.last_day or dl.pacer.today()
        loaded.candles += len(parsed.candles) + len(parsed.rejected)
        loaded.rejected.extend(parsed.rejected)
        loaded.longest_run = max(loaded.longest_run, parsed.longest_run)
        loaded.bars.extend(
            to_raw(bar, dl.basis.factor(instrument_id, trade_date_of(bar.start), fetched_on))
            for bar in aggregate(symbol, screen(parsed))
        )
        first = last + timedelta(days=1)
    if loaded.first > end:  # clamped past the period: the plan serves none of it
        raise MinuteHistoryError("history_plan_limit")
    return loaded


def _clamp(dl: Download, first: date) -> date:
    """The next first date after a refusal for the plan's depth: where the plan serves, or a
    day later when that was refused too; `history_plan_limit` after `max_clamps` refusals."""
    progress, report = dl.progress, dl.report
    if report.clamps >= dl.limits.max_clamps:
        raise MinuteHistoryError("history_plan_limit")
    report.clamps += 1
    first = max(first + timedelta(days=1), dl.pacer.today() - timedelta(days=dl.limits.plan_days))
    if progress.available_from is None or first > progress.available_from:
        progress.available_from = first
        progress.save()
    report.available_from = progress.available_from
    return first


def _parse_answer(
    response: Mapping[str, object], limits: HistoryLimits, first: date, last: date, carry: int
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
    parsed = parse_candles(raw, carry)

    # a rejected row counts wherever it claims to be: an answer is judged whole
    inside = [c for c in parsed.candles if first <= trade_date_of(c.at) <= last]
    return Parsed(inside, parsed.rejected, parsed.longest_run, parsed.trailing_run)


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
    pacer: Pacer | None = None,
    resume: bool = False,
    retry_failed: bool = False,
    holder: dict[str, HistoryReport] | None = None,
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
    with the progress saved. `holder["report"]` receives the report as soon as it exists, so a
    run stopped by an exception can still save what it did."""
    limits = limits or HistoryLimits()
    progress = progress if progress is not None else Progress()
    start, end = _begin(progress, start, end, resume, limits.plan_days)
    report = HistoryReport(start, end, symbols=len(instruments))
    report.available_from = progress.available_from
    if holder is not None:
        holder["report"] = report
    dl = Download(
        history,
        store,
        pacer or history_pacer(limits, progress),
        limits,
        progress,
        report,
        basis,
    )
    in_row = 0
    # symbols that failed last time go last, so a few bad ones cannot block a resume forever
    for symbol in sorted(instruments, key=lambda s: (s in progress.failed, s)):
        first = max(start, progress.available_from or start)
        if resume and _skipped(dl, symbol, first, end, retry_failed):
            continue
        code = _attempt(dl, str(instruments[symbol]), symbol, first, end)
        # only refused calls in a row point at a dead session; a symbol's bad data does not
        in_row = in_row + 1 if code == "history_failed" else 0
        report.stopped = _stop(code, in_row, limits)
        if report.stopped:
            break
        if dl.batch.full(limits):
            _flush(dl)
    _flush(dl)
    return report


def _skipped(dl: Download, symbol: str, first: date, end: date, retry: bool) -> bool:
    """Whether a resume skips `symbol` (counted in the report): loaded over the period, or
    failed on its data and not asked to `retry`."""
    progress, report = dl.progress, dl.report
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


def _attempt(dl: Download, instrument_id: str, symbol: str, first: date, end: date) -> str | None:
    """Load one symbol into the batch; None when it loaded, else the code it failed with (a
    symbol failure is recorded, a run stop is left to the caller). Its rejected candles go to
    the ledger either way."""
    if first > end:
        return "history_plan_limit"  # the plan serves nothing of the period
    progress, report, batch = dl.progress, dl.report, dl.batch
    try:
        loaded = _load_symbol(dl, instrument_id, symbol, first, end)
    except MinuteHistoryError as exc:
        if exc.code not in _RUN_STOPS:
            report.failed[symbol] = progress.failed[symbol] = exc.code
            progress.save()
        return exc.code
    finally:
        report.calls = dl.pacer.made
    rules = BarBuilder()
    for r in loaded.rejected:
        bucket = rules.bar_start(r.at) if r.at is not None else None
        day = trade_date_of(r.at) if r.at is not None else None
        batch.rejects.append(RejectRow(instrument_id, symbol, day, bucket, r.rule))
        report.rejects[r.rule] += 1
    if loaded.too_many_rejects(dl.limits):
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


def _flush(dl: Download) -> None:
    """Check the batch's days against the raw daily bars, write the ones that fit in one write
    (each symbol's stored days over its period are replaced, so a refused day keeps no older
    bar), append the ledger and only then mark the symbols done."""
    store, basis, batch, progress, report = dl.store, dl.basis, dl.batch, dl.progress, dl.report
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


# --- the command line ------------------------------------------------------------------------


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
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
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real Fyers and DB
    """python -m app.hat.data.minute_history [--days N] [--resume [--retry-failed]]

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
    with run_lock(root / PROGRESS_FILE.with_suffix(".lock")):
        return _download(args, days, limits, root, _basis(settings), fyers_history)


def _basis(settings: HatSettings) -> RawBasis:  # pragma: no cover - real DB
    """The factors the adjusted views apply (`chains.stored_adjustment`: once per chain, under
    its current id) and the raw daily bars of the data root."""
    from app.database import db_session_scope

    from .chains import stored_adjustment

    with db_session_scope(writes_data=True) as db:
        actions = stored_adjustment(db).factors
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    return RawBasis(actions, store_daily(store), settings.history_envelope_tol)


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
    from .universe import active_members  # the models: only the command line needs them

    with db_session_scope(writes_data=True) as db:
        members = active_members(db, with_broker_symbol=True)
    instruments = {m.broker_symbol: m.instrument_id for m in members if m.broker_symbol}
    unmapped = sum(1 for m in members if m.broker_symbol is None)
    today = datetime.now(IST).date()
    holder: dict[str, HistoryReport] = {}
    try:
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
            holder=holder,
        )
    except BaseException as exc:  # save the coverage so far, then stop as before
        if "report" in holder:
            holder["report"].stopped = stop_reason(exc)
            _save(root, holder["report"], unmapped)
        raise
    _save(root, report, unmapped)
    return 1 if report.stopped or report.failed else 0


def _save(root: Path, report: HistoryReport, unmapped: int) -> None:  # pragma: no cover
    text = report.summary() + f"active members without a Fyers symbol: {unmapped}\n"
    sys.stdout.write(text)
    save_report(root / PROGRESS_FILE.parent, text, datetime.now(IST))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
