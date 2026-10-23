"""The market a stored backtest reads (09 §1, HAT-BT-011, HAT-BT-012), loaded point in time.

Bars come from the adjusted view of the Parquet store (`bars_daily`: raw bars times the stored
corporate-action factors), features from the `features_daily` partitions the nightly job wrote,
the regime index from `index_daily`, and membership from the universe's intervals, delisted
instruments included. Nothing is filtered by what happened later: the day loop reads each input
only up to the date it decides for. The features must be the ones the nightly job wrote for the
corporate actions the bars are adjusted with: the job's stored inputs digest must equal the
digest of the stored actions over the whole store (HAT-DATA-077, PR 67).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
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
from ..data.features import STATE_FILE, inputs_digest
from ..data.store import DATASET_BARS, DATASET_FEATURES, DataStore
from ..engine.backtest import LOOKBACK_SESSIONS, BacktestError, MarketData
from ..engine.portfolio.universe import Interval
from ..models import HatIndustry, HatInstrument, HatUniverseMember

__all__ = ["load_market"]

FEATURES_LOCK = ".features.lock"  # the lock `data.features.run_features` holds while it writes

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


def _features_digest(store: DataStore) -> str | None:
    """The inputs digest the last complete `features` run recorded; None when none did."""
    try:
        state = json.loads((store.root / DATASET_FEATURES / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    digest = state.get("inputs_digest") if isinstance(state, dict) else None
    return digest if isinstance(digest, str) else None


def _current_features(store: DataStore, digest: str, days: list[date]) -> pl.DataFrame:
    """The features of `days`, read under the features job's lock (shared), so a run that is
    rewriting partitions finishes first; `backtest_features_stale` unless the job's recorded
    digest is `digest` and every day has a partition."""
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / FEATURES_LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if _features_digest(store) != digest or not all(
            store.partition_files(DATASET_FEATURES, day) for day in days
        ):
            raise BacktestError("backtest_features_stale")
        return _features(store, days)


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
    return digest.hexdigest()


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
    session in the period or no features, and `backtest_features_stale` when the features were
    not written for the stored corporate actions over every instrument (the nightly job's
    digest, `universe` None) or a session of the window has none: the loop would read prices
    adjusted one way and features computed another, or miss a night's scan, which the nightly
    job never does."""
    days = [d for d in store.days(DATASET_BARS) if d <= end]
    first = bisect_left(days, start)
    if first >= len(days):
        raise BacktestError("backtest_no_data")
    window = days[max(0, first - LOOKBACK_SESSIONS) :]
    actions = stored_factors(session)
    expected = inputs_digest(actions, None)
    features = _current_features(store, expected, days[max(0, first - 1) :])
    members = _members(session, universe_id)
    instruments = {m.instrument_id for m in members}
    with store.connect(actions) as con:
        bars = con.execute(_BARS_SQL, [window[0], end]).pl()
        closes = con.execute(_INDEX_SQL, [index, end]).fetchall()
    wanted = {str(i) for i in instruments}
    bars = bars.filter(pl.col("instrument_id").is_in(sorted(wanted)))
    series, delisted = _instruments(session, instruments)
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
        "actions_digest": expected,
        "content_digest": _content_digest(market),
    }
    return market, snapshot
