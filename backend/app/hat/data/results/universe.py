"""Which instruments the results store covers: those that pass the S2 EP universe
(`momentum-breakout-ep.yaml`: series EQ or BE, HAT-CRIT-100, and a 20-session mean traded value
of at least 10 crore, HAT-CRIT-101) on any session from `since`, NIFTY 500 members first.

Read from the raw daily bars (the latest ingest run's row per instrument and day, traded value or
close x volume where it is null, as `min_turnover` reads it); the series is the day's own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...models import HatIndexMember, HatInstrument, HatInstrumentSymbolHistory
from ..store import DATASET_BARS, DataStore, latest_rows

SERIES = ("EQ", "BE")
TURNOVER_DAYS = 20
TURNOVER_INR = 10 * 10**7  # 10 crore
BAR_SCHEMA = {
    "instrument_id": pl.String,
    "series": pl.String,
    "close": pl.Decimal(18, 4),
    "volume": pl.Int64,
    "traded_value": pl.Decimal(18, 4),
    "ingest_run_id": pl.String,
    "date": pl.Date,
}
NIFTY_500 = "NIFTY 500"
LOOKBACK_DAYS = 45  # calendar days before `since` that hold 20 sessions


@dataclass(frozen=True)
class Member:
    instrument_id: str
    isin: str
    symbols: tuple[str, ...]  # the current symbol first, then earlier ones since `since`
    nifty500: bool
    first_pass: date


def passing(frame: pl.DataFrame, since: date) -> pl.DataFrame:
    """Per instrument passing on some session on or after `since`, its first such session:
    `frame` holds daily bars (`date`, `instrument_id`, `series`, `close`, `volume`,
    `traded_value`, `ingest_run_id`) from enough sessions before `since` to fill the window."""
    bars = latest_rows(frame).with_columns(
        value=pl.coalesce(
            pl.col("traded_value").cast(pl.Float64),
            pl.col("close").cast(pl.Float64) * pl.col("volume").cast(pl.Float64),
        )
    )
    bars = bars.sort("instrument_id", "date").with_columns(
        mean=pl.col("value").rolling_mean(TURNOVER_DAYS).over("instrument_id")
    )
    ok = bars.filter(
        (pl.col("date") >= since)
        & pl.col("series").is_in(SERIES)
        & (pl.col("mean") >= TURNOVER_INR)
    )
    return ok.group_by("instrument_id").agg(pl.col("date").min().alias("first_pass"))


def read_bars(store: DataStore, start: date, end: date) -> pl.DataFrame:
    columns = ["instrument_id", "series", "close", "volume", "traded_value", "ingest_run_id"]
    frames = []
    for day in store.days(DATASET_BARS):
        if start <= day <= end:
            for path in store.partition_files(DATASET_BARS, day):
                frames.append(pl.read_parquet(path, columns=columns).with_columns(date=pl.lit(day)))
    if not frames:
        return pl.DataFrame(schema=BAR_SCHEMA)
    return pl.concat(frames, how="vertical_relaxed")


def members(session: Session, store: DataStore, since: date, until: date) -> list[Member]:
    """The covered instruments, NIFTY 500 point-in-time members (a membership in force on some
    day from `since` to `until`) first, then by first passing session."""
    found = passing(read_bars(store, since - timedelta(days=LOOKBACK_DAYS), until), since)
    first = {row["instrument_id"]: row["first_pass"] for row in found.iter_rows(named=True)}
    if not first:
        return []
    ids = list(first)
    instruments = {
        str(i.id): i
        for i in session.scalars(select(HatInstrument).where(HatInstrument.id.in_(ids)))
    }
    nifty = {
        str(i)
        for i in session.scalars(
            select(HatIndexMember.instrument_id).where(
                HatIndexMember.index_name == NIFTY_500,
                HatIndexMember.effective_from <= until,
                or_(HatIndexMember.effective_to.is_(None), HatIndexMember.effective_to > since),
            )
        )
    }
    history: dict[str, list[tuple[date, str]]] = {}
    for row in session.execute(
        select(
            HatInstrumentSymbolHistory.instrument_id,
            HatInstrumentSymbolHistory.symbol,
            HatInstrumentSymbolHistory.valid_from,
        ).where(
            HatInstrumentSymbolHistory.instrument_id.in_(ids),
            or_(
                HatInstrumentSymbolHistory.valid_to.is_(None),
                HatInstrumentSymbolHistory.valid_to >= since,
            ),
        )
    ):
        history.setdefault(str(row.instrument_id), []).append((row.valid_from, row.symbol))
    out = []
    for instrument_id, when in first.items():
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue
        earlier = [s for _d, s in sorted(history.get(instrument_id, []), reverse=True)]
        out.append(
            Member(
                instrument_id,
                instrument.isin,
                _unique([instrument.symbol, *earlier]),
                instrument_id in nifty,
                when,
            )
        )
    return sorted(out, key=lambda m: (not m.nifty500, m.first_pass, m.symbols[0]))


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))
