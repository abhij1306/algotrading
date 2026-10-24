"""What the daily cycle reads from the Parquet store (PR 17): the scan's inputs, the live
trigger's daily inputs, the close-phase exit views and the official closes of a day.

Everything is point in time: a read for trade date d uses sessions before d (the nightly scan and
the live trigger) or the official closes of d itself (the close gap). Prices come from the
total-return view (`bars_daily`: the price-changing actions and the stored dividends, CA3, on the
scale of the latest bar); ticks from the unadjusted closes, as NSE prices them (HAT-EXEC-053). A
missing value is None, never a guess, so a rule that needs it fails closed.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from decimal import Decimal

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.instruments import current_band
from ..data.lineage import stored_adjustment
from ..data.store import (
    DATASET_BARS,
    DATASET_FEATURES,
    DataStore,
)
from ..data.ticks import tick_for_date
from ..engine.exits import ExitView, view_from_rules
from ..engine.scan import ScanInputs
from ..engine.stops import lowest, sma
from ..engine.strategy.config import ExitRule, StopRule
from ..engine.trigger import DailyFeeds, DailyInputs, daily_inputs
from ..models import HatInstrument
from ..money import exact, require_exact

__all__ = [
    "DailyBook",
    "DailyData",
    "held_sessions",
    "SCAN_SESSIONS",
    "index_closes",
    "official_closes",
    "scan_inputs",
    "sessions_before",
]

SCAN_SESSIONS = 700  # the scan's window: enough for the 52-week and 200-day criteria
DAILY_SESSIONS = 260  # the live inputs: 200-day SMAs, 50-day volume, swing lows

_ADJ_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date,
       CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value,
       CAST(raw_close AS DOUBLE) AS raw_close
FROM bars_daily WHERE date BETWEEN ? AND ? ORDER BY instrument_id, date
"""
# The two closes `reference_close` can pick, per instrument: the last before the month start and
# the latest (each date's close from its newest ingest run; a date without a close is skipped).
_RAW_SQL = """
WITH latest AS (
    SELECT instrument_id, date, close FROM bars_daily_raw WHERE date BETWEEN ? AND ?
    QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1
)
SELECT CAST(instrument_id AS VARCHAR),
       max(date) FILTER (WHERE date < ?),
       CAST(arg_max(close, date) FILTER (WHERE date < ?) AS VARCHAR),
       max(date),
       CAST(arg_max(close, date) AS VARCHAR)
FROM latest WHERE close IS NOT NULL GROUP BY instrument_id
"""
_INDEX_SQL = """
SELECT date, CAST(close AS VARCHAR) FROM index_daily
WHERE index_name = ? AND date <= ? ORDER BY date
"""


def sessions_before(store: DataStore, day: date, count: int, *, inclusive: bool = False) -> list:
    """The last `count` stored sessions before `day` (through it when `inclusive`), oldest first."""
    days = [d for d in store.days(DATASET_BARS) if d < day or (inclusive and d == day)]
    return days[-count:]


def index_closes(store: DataStore, index: str, through: date) -> list[Decimal]:
    """The closes of `index` dated on or before `through`, oldest first."""
    with store.connect() as con:
        rows = con.execute(_INDEX_SQL, [index, through]).fetchall()
    return [require_exact(close, "close") for _, close in rows]


def _features(store: DataStore, day: date) -> pl.DataFrame | None:
    frame = store.read_partition(DATASET_FEATURES, day)
    if frame.height == 0:
        return None
    return frame.with_columns(pl.col("instrument_id").cast(pl.String), pl.lit(day).alias("date"))


def _series(session: Session) -> dict[str, str]:
    return {str(i): s for i, s in session.execute(select(HatInstrument.id, HatInstrument.series))}


def scan_inputs(store: DataStore, session: Session, as_of: date, index: str) -> ScanInputs:
    """The inputs of a nightly scan as of the session `as_of` (the members are replaced by the
    bot's universe in `scan_and_publish`). Price bands are not read: no criterion of the MVP
    bots uses them in the scan, as in the backtest."""
    window = sessions_before(store, as_of, SCAN_SESSIONS, inclusive=True)
    if not window:
        bars = pl.DataFrame()
    else:
        with store.connect(stored_adjustment(session)) as con:
            bars = con.execute(_ADJ_SQL, [window[0], as_of]).pl()
    return ScanInputs(
        as_of=as_of,
        members=(),
        bars=bars,
        regime_closes=index_closes(store, index, as_of),
        features=_features(store, as_of),
        series=_series(session),
    )


def official_closes(store: DataStore, day: date) -> dict[uuid.UUID, Decimal]:
    """Each instrument's official close of `day` (the latest ingest run's row)."""
    latest = store.read_bars(day)
    out: dict[uuid.UUID, Decimal] = {}
    for instrument_id, close in zip(latest["instrument_id"], latest["close"], strict=True):
        value = exact(close, floats=True)
        if instrument_id and value is not None:
            out[uuid.UUID(instrument_id)] = value
    return out


class DailyData:
    """The daily view of trade date `trade_date`: adjusted bars of the sessions before it, the
    unadjusted closes for the tick, and the previous session's features. Loaded once per trade
    date and read from the trigger, exit and stop code (read-only after construction)."""

    def __init__(
        self,
        store: DataStore,
        session: Session,
        trade_date: date,
    ) -> None:
        self.trade_date = trade_date
        window = sessions_before(store, trade_date, DAILY_SESSIONS)
        adjustment = stored_adjustment(session)
        # a position bought under a linked older ISIN reads its chain's bars, features and tick
        self._chain: dict[str, str] = dict(adjustment.lineage)
        if window:
            with store.connect(adjustment) as con:
                bars = con.execute(_ADJ_SQL, [window[0], window[-1]]).pl()
                month_start = trade_date.replace(day=1)
                raw = con.execute(
                    _RAW_SQL, [window[0], window[-1], month_start, month_start]
                ).fetchall()
        else:
            bars, raw = pl.DataFrame(), []
        self._bars: dict[str, pl.DataFrame] = (
            {str(k[0]): v for k, v in bars.partition_by("instrument_id", as_dict=True).items()}
            if bars.height
            else {}
        )
        self._raw: dict[str, tuple[list[date], list[Decimal | None]]] = {}
        for instrument_id, *picks in raw:
            pairs = [
                (day, exact(close, floats=True))
                for day, close in zip(picks[::2], picks[1::2], strict=True)
                if day
            ]
            self._raw[instrument_id] = ([d for d, _ in pairs], [c for _, c in pairs])
        features = _features(store, window[-1]) if window else None
        self._features: dict[str, dict[str, object]] = (
            {str(r["instrument_id"]): r for r in features.to_dicts()}
            if features is not None
            else {}
        )

    def chain_key(self, instrument_id: uuid.UUID) -> str:
        """The id the adjusted view gives the instrument's bars: its chain's current ISIN when
        it is a linked older one (`lineage`), else its own."""
        key = str(instrument_id)
        return self._chain.get(key, key)

    def bars(self, instrument_id: uuid.UUID) -> pl.DataFrame:
        """The instrument's adjusted daily bars before the trade date (empty when unknown)."""
        return self._bars.get(self.chain_key(instrument_id), pl.DataFrame())

    def tick(self, instrument_id: uuid.UUID) -> Decimal | None:
        dates, closes = self._raw.get(self.chain_key(instrument_id), ([], []))
        return tick_for_date(dates, closes, self.trade_date)

    def previous_raw_close(self, instrument_id: uuid.UUID) -> tuple[date, Decimal] | None:
        """The last session before the trade date and its raw close (the price as traded);
        None without one."""
        frame = self.bars(instrument_id)
        if frame.height == 0:
            return None
        close = exact(frame["raw_close"][-1], floats=True)
        return None if close is None else (frame["date"][-1], close)

    def feature(self, instrument_id: uuid.UUID, name: str) -> Decimal | None:
        return exact(self._features.get(self.chain_key(instrument_id), {}).get(name), floats=True)

    def _closes(self, instrument_id: uuid.UUID) -> list[Decimal]:
        frame = self.bars(instrument_id)
        return (
            [require_exact(v, "close", floats=True) for v in frame["close"].to_list()]
            if frame.height
            else []
        )

    def daily(
        self, session: Session, instrument_id: uuid.UUID, stop: StopRule | None = None
    ) -> DailyInputs:
        """The trigger's `DailyInputs` of one instrument (HAT-CRIT-115, HAT-DATA-115), with the
        ATR and swing lows of the candidate's own `stop` rule at its own length."""
        found, band = current_band(session, instrument_id, self.trade_date)
        frame = self.bars(instrument_id)
        if frame.height == 0:
            return DailyInputs(tick=self.tick(instrument_id), band_known=found, band_pct=band)
        feeds = DailyFeeds(
            tick=self.tick(instrument_id),
            adv20=self.feature(instrument_id, "adv20"),
            adtr20=self.feature(instrument_id, "adtr20"),
            band_known=found,
            band_pct=band,
        )
        return daily_inputs(frame, self.trade_date, feeds, stop)

    def sma(self, instrument_id: uuid.UUID, length: int, live: Decimal) -> Decimal | None:
        """The `length`-session SMA with the live price standing in for today's close (the
        close-phase rules read the live price as the close proxy)."""
        closes = self._closes(instrument_id)[-(length - 1) :] if length > 1 else []
        return sma([*closes, live], length)

    def swing_low(self, instrument_id: uuid.UUID, lookback: int) -> Decimal | None:
        """The lowest low of the `lookback` sessions before the trade date."""
        frame = self.bars(instrument_id).tail(lookback)
        lows = frame["low"].to_list() if frame.height else []
        return lowest([require_exact(v, "low", floats=True) for v in lows], lookback)

    def exit_view(
        self,
        instrument_id: uuid.UUID,
        price: Decimal,
        rules: Sequence[ExitRule],
        days_held: int,
    ) -> ExitView:
        """The view of a position at the live `price` under its pinned exit `rules`."""
        return view_from_rules(
            price,
            rules,
            days_held,
            lambda length: self.sma(instrument_id, length, price),
            lambda lookback: self.swing_low(instrument_id, lookback),
        )


def held_sessions(trading_days: Iterable[date], opened: date, today: date) -> int:
    """Trading sessions after the entry day up to `today` (0 on the entry day)."""
    return sum(1 for d in set(trading_days) if opened < d <= today)


class DailyBook:
    """One `DailyData` per trade date, loaded on first use and shared by the trigger, stop and
    exit code of that day. `reset` drops the cache (after the nightly load writes new bars)."""

    def __init__(self, store: DataStore, session_factory: Callable[[], Session]) -> None:
        self._store = store
        self._session_factory = session_factory
        self._days: dict[date, DailyData] = {}
        self._lock = threading.Lock()

    def day(self, trade_date: date) -> DailyData:
        with self._lock:
            found = self._days.get(trade_date)
            if found is None:
                with self._session_factory() as session:
                    found = DailyData(self._store, session, trade_date)
                self._days = {trade_date: found}  # only the current day is kept
            return found

    def reset(self) -> None:
        with self._lock:
            self._days = {}
