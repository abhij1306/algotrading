"""The market a stored backtest reads (09 §1, HAT-BT-011, HAT-BT-012), loaded point in time.

Bars come from the adjusted view of the Parquet store (`bars_daily`: raw bars times the stored
corporate-action factors), features from the `features_daily` partitions the nightly job wrote,
the regime index from `index_daily`, and membership from the universe's intervals, delisted
instruments included. Nothing is filtered by what happened later: the day loop reads each input
only up to the date it decides for.
"""

from __future__ import annotations

import uuid
from bisect import bisect_left
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.corporate_actions import stored_factors
from ..data.features import inputs_digest
from ..data.store import DATASET_BARS, DATASET_FEATURES, DataStore
from ..engine.backtest import LOOKBACK_SESSIONS, BacktestError, MarketData
from ..engine.portfolio.universe import Interval
from ..models import HatIndustry, HatInstrument, HatUniverseMember

__all__ = ["load_market"]

_BARS_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date,
       CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value
FROM bars_daily WHERE date BETWEEN ? AND ? ORDER BY instrument_id, date
"""
_INDEX_SQL = """
SELECT date, CAST(close AS VARCHAR) FROM index_daily
WHERE index_name = ? AND date <= ? ORDER BY date
"""


def _members(session: Session, universe_id: uuid.UUID) -> tuple[Interval, ...]:
    rows = session.scalars(
        select(HatUniverseMember)
        .where(HatUniverseMember.universe_id == universe_id)
        .order_by(HatUniverseMember.instrument_id, HatUniverseMember.effective_from)
    ).all()
    return tuple(Interval(r.instrument_id, r.effective_from, r.effective_to) for r in rows)


def _industries(session: Session, instruments: Iterable[uuid.UUID]) -> dict[str, str]:
    """Each instrument's newest industry (the classification is not kept point in time)."""
    wanted = set(instruments)
    found: dict[str, str] = {}
    for row in session.scalars(select(HatIndustry).order_by(HatIndustry.effective_from)):
        if row.instrument_id in wanted:
            found[str(row.instrument_id)] = row.industry
    return found


def _instruments(
    session: Session, instruments: Iterable[uuid.UUID]
) -> tuple[dict[str, str], dict[str, date]]:
    wanted = set(instruments)
    series: dict[str, str] = {}
    delisted: dict[str, date] = {}
    for row in session.scalars(select(HatInstrument)):
        if row.id in wanted:
            series[str(row.id)] = row.series
            if row.delisted_on is not None:
                delisted[str(row.id)] = row.delisted_on
    return series, delisted


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


def load_market(
    store: DataStore,
    session: Session,
    *,
    universe_id: uuid.UUID,
    index: str,
    start: date,
    end: date,
) -> tuple[MarketData, dict[str, Any]]:
    """The market of a run of `start..end` on the universe, and its data snapshot (what was
    read, for `hat_backtest_run.data_snapshot`). Raises `BacktestError` when the store has no
    session in the period or no features."""
    days = [d for d in store.days(DATASET_BARS) if d <= end]
    first = bisect_left(days, start)
    if first >= len(days):
        raise BacktestError("backtest_no_data")
    window = days[max(0, first - LOOKBACK_SESSIONS) :]
    actions = stored_factors(session)
    members = _members(session, universe_id)
    instruments = {m.instrument_id for m in members}
    with store.connect(actions) as con:
        bars = con.execute(_BARS_SQL, [window[0], end]).pl()
        closes = con.execute(_INDEX_SQL, [index, end]).fetchall()
    wanted = {str(i) for i in instruments}
    bars = bars.filter(pl.col("instrument_id").is_in(sorted(wanted)))
    features = _features(store, days[max(0, first - 1) :])
    series, delisted = _instruments(session, instruments)
    last_traded: dict[str, date] = {}
    for instrument_id in delisted:
        dates = bars.filter(pl.col("instrument_id") == instrument_id)["date"]
        if dates.len():
            last_traded[instrument_id] = dates.max()  # type: ignore[assignment]
    market = MarketData(
        days=tuple(window),
        bars=bars,
        features=features,
        index_closes={index: [(d, Decimal(c)) for d, c in closes]},
        members=members,
        industries=_industries(session, instruments),
        series=series,
        last_traded=last_traded,
    )
    snapshot = {
        "store_first": window[0].isoformat(),
        "store_last": window[-1].isoformat(),
        "sessions": len(window),
        "bars": bars.height,
        "features": features.height,
        "index": index,
        "index_points": len(closes),
        "members": len(members),
        "actions_digest": inputs_digest(actions, None),
    }
    return market, snapshot
