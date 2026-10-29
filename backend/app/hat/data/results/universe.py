"""Which instruments the results store covers (`scope.py`): by default the NIFTY 500
point-in-time members; or those that pass the S2 EP universe (`momentum-breakout-ep.yaml`: series
EQ or BE, HAT-CRIT-100, and a 20-session mean traded value of at least 10 crore, HAT-CRIT-101) on
any session from `since`, NIFTY 500 members first.

The EP universe is read from the raw daily bars (the latest ingest run's row per instrument and
day, traded value or close x volume where it is null, as `min_turnover` reads it); the series is
the day's own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...models import (
    HatIndexMember,
    HatIndexMembership,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatIsinLineage,
)
from ..store import DATASET_BARS, DataStore, latest_rows
from .scope import UNIVERSE_NIFTY500, coverage_gap, nifty500_first

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
logger = logging.getLogger(__name__)
LOOKBACK_DAYS = 45  # calendar days before `since` that hold 20 sessions


@dataclass(frozen=True)
class Member:
    instrument_id: str
    isin: str
    symbols: tuple[str, ...]  # the current symbol first, then earlier ones since `since`
    nifty500: bool
    first_pass: date
    older: tuple[str, ...] = ()  # the instruments of its earlier ISINs (`successors`)


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


def members(
    session: Session, store: DataStore, since: date, until: date, universe: str = UNIVERSE_NIFTY500
) -> list[Member]:
    """The covered instruments (`scope.py`): the NIFTY 500 point-in-time members with a
    membership in force on some day from `since` to `until` (`nifty500`), or the instruments
    passing the S2 EP universe on some session from `since` (`ep`, members first); then by first
    day."""
    rows = session.execute(
        select(
            HatIndexMember.instrument_id,
            HatIndexMember.effective_from,
            HatIndexMember.effective_to,
        ).where(HatIndexMember.index_name == NIFTY_500)
    )
    nifty_first = nifty500_first(((str(r[0]), r[1], r[2]) for r in rows), since, until)
    if universe == UNIVERSE_NIFTY500:
        record = session.get(HatIndexMembership, NIFTY_500)
        gap = coverage_gap(record.covered_from if record else None, since)
        if gap:
            raise ValueError(gap)
        if record is not None and record.covered_to < until:
            logger.warning(
                "NIFTY 500 membership history ends %s: later joiners are not covered",
                record.covered_to,
            )
        first = nifty_first
    else:
        found = passing(read_bars(store, since - timedelta(days=LOOKBACK_DAYS), until), since)
        first = {row["instrument_id"]: row["first_pass"] for row in found.iter_rows(named=True)}
    if not first:
        return []
    head = successors(session)
    ids = list({*first, *head, *head.values()})
    instruments = {
        str(i.id): i
        for i in session.scalars(select(HatInstrument).where(HatInstrument.id.in_(ids)))
    }
    nifty = set(nifty_first)
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
    chains: dict[str, list[str]] = {}
    for instrument_id in first:
        chains.setdefault(head.get(instrument_id, instrument_id), []).append(instrument_id)
    out = []
    for latest, olds in chains.items():
        instrument = instruments.get(latest)
        if instrument is None:
            continue
        symbols = [instrument.symbol]
        older = sorted(i for i, h in head.items() if h == latest)
        for one in dict.fromkeys([latest, *olds, *older]):
            symbols += [s for _d, s in sorted(history.get(one, []), reverse=True)]
            if one in instruments:
                symbols.append(instruments[one].symbol)
        out.append(
            Member(
                latest,
                instrument.isin,
                _unique(symbols),
                any(i in nifty for i in olds),
                min(first[i] for i in olds),
                tuple(older),
            )
        )
    return sorted(out, key=lambda m: (not m.nifty500, m.first_pass, m.symbols[0]))


def successors(session: Session) -> dict[str, str]:
    """Each instrument that changed ISIN (`hat_isin_lineage`, linked) -> its latest successor: a
    company is covered once, as its current instrument, which holds every filing of every ISIN
    it had (NSE's listing keeps naming a filing by the old ISIN long after a change)."""
    newer = {
        str(old): str(new)
        for old, new in session.execute(
            select(HatIsinLineage.old_instrument_id, HatIsinLineage.new_instrument_id).where(
                HatIsinLineage.status == "linked"
            )
        )
    }
    return latest_of(newer, newer)


def latest_of(newer: dict[str, str], ids: Iterable[str]) -> dict[str, str]:
    """Each of `ids` with a successor in `newer` (old -> new) -> the end of its chain."""
    out = {}
    for start in ids:
        at, seen = start, {start}
        while at in newer and newer[at] not in seen:
            at = newer[at]
            seen.add(at)
        if at != start:
            out[start] = at
    return out


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))
