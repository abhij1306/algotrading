"""The market a backtest reads (HAT-BT-011, HAT-BT-012), loaded point in time, and the day loop's
views of it.

Bars come from the adjusted view of the Parquet store (`bars_daily`: raw bars times the stored
corporate-action factors), features from the `features_daily` partitions the nightly job wrote,
the regime index from `index_daily`, and membership from the universe's intervals, delisted
instruments included. Nothing is filtered by what happened later: the day loop reads each input
only up to the date it decides for (`MarketView`). The features must be the ones the nightly
job wrote for the corporate actions the bars are adjusted with: the job's stored inputs digest
must equal the digest of the stored actions over the whole store (HAT-DATA-077, PR 67). A run
under the `policy` regime rule also reads the regime engine's inputs: the NIFTY 500 and India VIX
closes and the features job's breadth of each session (PR 68).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.corporate_actions import stored_factors
from ..data.features import STATE_FILE, inputs_digest
from ..data.identity import series_changes
from ..data.store import DATASET_BARS, DATASET_BREADTH, DATASET_FEATURES, DataStore
from ..data.ticks import tick_for_date
from ..engine.portfolio import regime
from ..engine.portfolio.universe import Interval
from ..engine.stops import mean_true_range
from ..models import HatIndustry, HatInstrument, HatUniverseMember
from .spec import BacktestError

__all__ = [
    "LOOKBACK_SESSIONS",
    "Bar",
    "MarketData",
    "MarketView",
    "classifications",
    "industry_on",
    "load_market",
]

# The scan reads at most 652 sessions back (a 400-session SMA compared 252 sessions earlier);
# the window it is given holds more, so its watchlist equals a scan of the full history.
LOOKBACK_SESSIONS = 700
_PRICE_PLACES = Decimal("0.0001")
_ZERO = Decimal(0)
_ALWAYS = date(1900, 1, 1)


@dataclass(frozen=True)
class MarketData:
    """Everything a run reads, point in time.

    `days`: every session of the store, sorted. `bars`: adjusted daily bars (`instrument_id`
    text, `date`, `open`, `high`, `low`, `close`, `volume`, `traded_value`, as doubles).
    `features`: the daily features (`date`, `instrument_id`, columns). `index_closes`:
    per index name, (date, close) oldest first. `members`: the universe's membership intervals,
    delisted instruments included. `industries`: each instrument's industry, either one name
    or its classifications as (effective_from, industry), read point in time. `series`: each
    instrument's series as change points (first day, series) from its bars, so a scan reads
    the series of its own day (`identity.series_on`), not today's. `last_traded`: the
    last session of each instrument delisted by the period's end. `breadth`: the features job's
    `breadth_pct` per session, read with the `regime.INDEX_NAME` and `regime.VIX_NAME` closes of
    `index_closes` only by a run under the `policy` regime rule (PR 68).
    """

    days: tuple[date, ...]
    bars: pl.DataFrame
    features: pl.DataFrame
    index_closes: Mapping[str, Sequence[tuple[date, Decimal]]]
    members: tuple[Interval, ...]
    industries: Mapping[str, str | Sequence[tuple[date, str]]] = field(default_factory=dict)
    series: Mapping[str, Sequence[tuple[date, str]]] = field(default_factory=dict)
    last_traded: Mapping[str, date] = field(default_factory=dict)
    # 5-minute bars of a session, keyed (instrument_id, date): (high, low) per bar in time order,
    # the 09:15 bar first, on the daily bars' (adjusted) scale. A session without them uses the
    # daily estimate (PR 60).
    intraday: Mapping[tuple[str, date], Sequence[tuple[Decimal, Decimal]]] = field(
        default_factory=dict
    )
    breadth: Mapping[date, Decimal] = field(default_factory=dict)


def classifications(data: MarketData, instrument_id: str) -> list[tuple[date, str]]:
    """The instrument's industry classifications, oldest first."""
    found = data.industries.get(instrument_id, "unknown")
    if isinstance(found, str):
        return [(_ALWAYS, found)]
    return sorted(found) or [(_ALWAYS, "unknown")]


def industry_on(data: MarketData, instrument_id: str, day: date) -> str:
    """The industry in force on `day`: the newest classification effective on or before it."""
    rows = [name for start, name in classifications(data, instrument_id) if start <= day]
    return rows[-1] if rows else "unknown"


def _price(value: object) -> Decimal | None:
    """A stored price as an exact Decimal of the store's 4 places; None when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        number = Decimal(repr(value))
    else:
        return None
    return number.quantize(_PRICE_PLACES) if number.is_finite() else None


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class MarketView:
    """The point-in-time reads the day loop makes of `MarketData` (HAT-BT-011): each session's
    tradeable bars as exact Decimals, an instrument's bars up to a day and the SMA, swing low,
    ATR and NSE tick live computes from them, the features of a session and the closes of the
    strategy's regime index (`index`). A bar without four positive prices is not tradeable and
    is left out."""

    def __init__(self, data: MarketData, index: str) -> None:
        self.days = tuple(sorted(set(data.days)))
        self.index = {d: n for n, d in enumerate(self.days)}
        self.bars = data.bars.sort("instrument_id", "date")
        self.bar = self._bars_by_day()
        self.history = self._history_by_instrument()
        self.by_day = self._bars_by_session()
        self.feature_rows: dict[date, dict[str, dict[str, Any]]] = {}
        self.features = {
            key[0]: part for key, part in data.features.partition_by("date", as_dict=True).items()
        }
        self.closes = list(data.index_closes.get(index, ()))

    def _bars_by_day(self) -> dict[tuple[str, date], Bar]:
        found: dict[tuple[str, date], Bar] = {}
        for row in self.bars.select("instrument_id", "date", "open", "high", "low", "close").rows():
            prices = [_price(v) for v in row[2:]]
            if any(p is None or p <= 0 for p in prices):
                continue  # a bar without four positive prices cannot be traded on
            found[(str(row[0]), row[1])] = Bar(*prices)  # type: ignore[arg-type]
        return found

    def _history_by_instrument(self) -> dict[str, tuple[list[date], list[Bar]]]:
        found: dict[str, tuple[list[date], list[Bar]]] = {}
        for (instrument_id, day), bar in sorted(self.bar.items(), key=lambda item: item[0]):
            dates, bars = found.setdefault(instrument_id, ([], []))
            dates.append(day)
            bars.append(bar)
        return found

    def _bars_by_session(self) -> dict[date, list[tuple[str, Bar]]]:
        found: dict[date, list[tuple[str, Bar]]] = {}
        for (instrument_id, day), bar in sorted(self.bar.items(), key=lambda item: item[0]):
            found.setdefault(day, []).append((instrument_id, bar))
        return found

    def prev(self, day: date) -> date | None:
        n = self.index[day]
        return self.days[n - 1] if n > 0 else None

    def closes_through(self, day: date) -> list[Decimal]:
        return [c for d, c in self.closes if d <= day]

    def feature(self, day: date, instrument_id: str, name: str) -> Decimal | None:
        rows = self.feature_rows.get(day)
        if rows is None:
            frame = self.features.get(day)
            rows = {} if frame is None else {str(r["instrument_id"]): r for r in frame.to_dicts()}
            self.feature_rows[day] = rows
        return _price(rows.get(instrument_id, {}).get(name))

    def bars_through(self, instrument_id: str, day: date, *, inclusive: bool = True) -> list[Bar]:
        """The instrument's bars dated on or before `day` (before it, if not `inclusive`)."""
        dates, bars = self.history.get(instrument_id, ([], []))
        end = bisect_right(dates, day) if inclusive else bisect_left(dates, day)
        return bars[:end]

    def tick(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's NSE tick on `day` (HAT-EXEC-053), from its closes before `day`, as
        the live trigger prices it; None without an earlier close."""
        bars = self.bars_through(instrument_id, day, inclusive=False)
        dates = self.history.get(instrument_id, ([], []))[0][: len(bars)]
        return tick_for_date(dates, [b.close for b in bars], day)

    def sma(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        bars = self.bars_through(instrument_id, day)
        if len(bars) < length:
            return None
        return sum((b.close for b in bars[-length:]), _ZERO) / length

    def lowest_low_before(self, instrument_id: str, before: date, lookback: int) -> Decimal | None:
        """The lowest low of the `lookback` sessions the instrument traded before `before`."""
        bars = self.bars_through(instrument_id, before, inclusive=False)
        return min(b.low for b in bars[-lookback:]) if len(bars) >= lookback else None

    def atr(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        """The mean true range of the `length` sessions up to `day`."""
        window = self.bars_through(instrument_id, day)[-(length + 1) :]
        return mean_true_range(
            [b.high for b in window], [b.low for b in window], [b.close for b in window], length
        )

    def window(self, as_of: date) -> pl.DataFrame:
        """The bars of the `LOOKBACK_SESSIONS` sessions up to `as_of`: what the scan reads."""
        first = self.days[max(0, self.index[as_of] - LOOKBACK_SESSIONS)]
        return self.bars.filter((pl.col("date") >= first) & (pl.col("date") <= as_of))

    def prev_close(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's last close before `day`; None without one."""
        dates, bars = self.history.get(instrument_id, ([], []))
        at = bisect_left(dates, day)
        return bars[at - 1].close if at > 0 else None


FEATURES_LOCK = ".features.lock"  # the lock `data.features.run_features` holds while it writes

_BARS_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date,
       CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value
FROM bars_daily WHERE date BETWEEN ? AND ? ORDER BY instrument_id, date
"""
# the series each bar traded under (one id per ISIN: the series is a dated attribute, audit D5)
_SERIES_SQL = """
SELECT CAST(instrument_id AS VARCHAR), date, series FROM bars_daily WHERE date BETWEEN ? AND ?
"""
# every index the run reads, in one statement, so a concurrent upsert cannot split them
_INDEX_SQL = """
SELECT index_name, date, CAST(close AS VARCHAR) FROM index_daily
WHERE list_contains(?, index_name) AND date <= ? ORDER BY index_name, date
"""


def _members(session: Session, universe_id: uuid.UUID) -> tuple[Interval, ...]:
    rows = session.scalars(
        select(HatUniverseMember)
        .where(HatUniverseMember.universe_id == universe_id)
        .order_by(HatUniverseMember.instrument_id, HatUniverseMember.effective_from)
    ).all()
    return tuple(Interval(r.instrument_id, r.effective_from, r.effective_to) for r in rows)


def _industries(
    session: Session, instruments: Iterable[uuid.UUID]
) -> dict[str, list[tuple[date, str]]]:
    """Each instrument's industry classifications as (effective_from, industry), oldest first:
    the run reads the one in force on each trade date, never a later reclassification."""
    wanted = set(instruments)
    found: dict[str, list[tuple[date, str]]] = {}
    for row in session.scalars(
        select(HatIndustry).order_by(HatIndustry.instrument_id, HatIndustry.effective_from)
    ):
        if row.instrument_id in wanted:
            found.setdefault(str(row.instrument_id), []).append((row.effective_from, row.industry))
    return found


def _delisted(session: Session, instruments: Iterable[uuid.UUID]) -> dict[str, date]:
    wanted = set(instruments)
    return {
        str(row.id): row.delisted_on
        for row in session.scalars(select(HatInstrument))
        if row.id in wanted and row.delisted_on is not None
    }


def _features(store: DataStore, days: Iterable[date]) -> pl.DataFrame:
    frames = [
        frame.with_columns(pl.lit(day).alias("date"))
        for day in days
        if (frame := store.read_partition(DATASET_FEATURES, day)).height
    ]
    if not frames:
        raise BacktestError("backtest_no_features")
    return pl.concat(frames, how="diagonal_relaxed").with_columns(
        pl.col("instrument_id").cast(pl.String)
    )


def _features_digest(store: DataStore) -> str | None:
    """The inputs digest the last complete `features` run recorded; None when none did."""
    try:
        state = json.loads((store.root / DATASET_FEATURES / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    digest = state.get("inputs_digest") if isinstance(state, dict) else None
    return digest if isinstance(digest, str) else None


def _current_features(
    store: DataStore, digest: str, days: list[date], breadth_days: list[date]
) -> tuple[pl.DataFrame, dict[date, Decimal]]:
    """The features of `days` and the breadth of `breadth_days`, read under the features job's
    lock (shared), so a run that is rewriting partitions finishes first and both come from one
    generation; `backtest_features_stale` unless the job's recorded digest is `digest` and
    every day has a partition."""
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / FEATURES_LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if _features_digest(store) != digest or not all(
            store.partition_files(DATASET_FEATURES, day) for day in days
        ):
            raise BacktestError("backtest_features_stale")
        return _features(store, days), _breadth(store, breadth_days)


def _breadth(store: DataStore, days: Iterable[date]) -> dict[date, Decimal]:
    """The features job's `breadth_pct` of each of `days` that has one, as the regime job
    reads it (`portfolio.jobs.read_breadth`)."""
    found: dict[date, Decimal] = {}
    for day in days:
        frame = store.read_partition(DATASET_BREADTH, day)
        if frame.height and frame["breadth_pct"][0] is not None:
            found[day] = Decimal(str(frame["breadth_pct"][0]))
    return found


def _content_digest(market: MarketData) -> str:
    """sha256 of what the run read: the bars, features, index closes, membership intervals and
    industry history, each in a fixed order. Two snapshots with the same row counts but
    different prices or members have different digests."""
    digest = hashlib.sha256()
    for frame in (market.bars, market.features):
        ordered = frame.select(sorted(frame.columns)).sort(["date", "instrument_id"])
        digest.update(ordered.write_csv().encode())
    for name, points in sorted(market.index_closes.items()):
        digest.update(repr((name, [(d.isoformat(), str(c)) for d, c in points])).encode())
    digest.update(
        repr(
            sorted((str(m.instrument_id), m.effective_from, m.effective_to) for m in market.members)
        ).encode()
    )
    digest.update(repr(sorted(market.industries.items())).encode())
    if market.breadth:
        digest.update(
            repr(sorted((d.isoformat(), str(v)) for d, v in market.breadth.items())).encode()
        )
    return digest.hexdigest()


def load_market(
    store: DataStore,
    session: Session,
    *,
    universe_id: uuid.UUID,
    index: str,
    start: date,
    end: date,
    regime_inputs: bool = False,
) -> tuple[MarketData, dict[str, Any]]:
    """The market of a run of `start..end` on the universe, and its data snapshot (what was
    read, for `hat_backtest_run.data_snapshot`). Raises `BacktestError` when the store has no
    session in the period or no features, and `backtest_features_stale` when the features were
    not written for the stored corporate actions over every instrument (the nightly job's
    digest, `universe` None) or a session of the window has none: the loop would read prices
    adjusted one way and features computed another, or miss a night's scan, which the nightly
    job never does. With `regime_inputs` it also reads the regime engine's index and VIX closes
    and the window's breadth, and raises `backtest_no_regime_inputs` when the store has none
    of one of them (PR 68)."""
    days = [d for d in store.days(DATASET_BARS) if d <= end]
    first = bisect_left(days, start)
    if first >= len(days):
        raise BacktestError("backtest_no_data")
    window = days[max(0, first - LOOKBACK_SESSIONS) :]
    actions = stored_factors(session)
    expected = inputs_digest(actions, None)
    features, breadth = _current_features(
        store, expected, days[max(0, first - 1) :], window if regime_inputs else []
    )
    members = _members(session, universe_id)
    instruments = {m.instrument_id for m in members}
    with store.connect(actions) as con:
        bars = con.execute(_BARS_SQL, [window[0], end]).pl()
        traded = con.execute(_SERIES_SQL, [window[0], end]).fetchall()
        names = sorted({index, *((regime.INDEX_NAME, regime.VIX_NAME) if regime_inputs else ())})
        rows = con.execute(_INDEX_SQL, [names, end]).fetchall()
    index_closes: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in names}
    for name, day, close in rows:
        index_closes[name].append((day, Decimal(close)))
    closes = index_closes[index]
    if regime_inputs and not (
        breadth and all(index_closes[n] for n in (regime.INDEX_NAME, regime.VIX_NAME))
    ):
        raise BacktestError("backtest_no_regime_inputs")
    wanted = {str(i) for i in instruments}
    bars = bars.filter(pl.col("instrument_id").is_in(sorted(wanted)))
    series = series_changes(r for r in traded if r[0] in wanted)
    delisted = _delisted(session, instruments)
    # Only an instrument delisted by the period's end has a last traded session in the run; one
    # delisted later simply has no more bars after `end`, which is not a delisting.
    last_traded: dict[str, date] = {}
    for instrument_id, delisted_on in delisted.items():
        if delisted_on > end:
            continue
        dates = bars.filter(
            (pl.col("instrument_id") == instrument_id) & (pl.col("date") <= delisted_on)
        )["date"]
        if dates.len():
            last_traded[instrument_id] = dates.max()  # type: ignore[assignment]
    market = MarketData(
        days=tuple(window),
        bars=bars,
        features=features,
        index_closes=index_closes,
        members=members,
        industries=_industries(session, instruments),
        series=series,
        last_traded=last_traded,
        breadth=breadth,
    )
    snapshot = {
        "store_first": window[0].isoformat(),
        "store_last": window[-1].isoformat(),
        "sessions": len(window),
        "bars": bars.height,
        "features": features.height,
        "index": index,
        "index_points": len(closes),
        **(
            {
                "breadth_points": len(breadth),
                "regime_index_points": len(index_closes[regime.INDEX_NAME]),
                "vix_points": len(index_closes[regime.VIX_NAME]),
            }
            if regime_inputs
            else {}
        ),
        "members": len(members),
        "actions_digest": expected,
        "content_digest": _content_digest(market),
    }
    return market, snapshot
