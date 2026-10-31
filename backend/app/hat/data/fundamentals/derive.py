"""The derive step: `hat_fundamental_metric` rebuilt from the stored filings (`book.py` holds the
rules and `formulas.py` the formulas), and the point-in-time reads of the store.

Per instrument, the moments are each distinct `known_at` of its filings, plus the close of the
latest session in the price store (so the latest valuation is current). A filing is known from its
`public_at`; a secondary-source page's figures are as restated when it was fetched
(`point_in_time` false), so they are known only from the later of `public_at` and the fetch. At
each moment the book holds the filings known by then; the metrics are computed with the last
close known then (a session's close is known from 15:30 IST that day). A metric row is written
when its value or period changes, so a read at any moment takes the latest row at or before it.
"""

from __future__ import annotations

import logging
import uuid
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from ...time import IST, as_utc
from .book import Book, KnownFact, Metric, Quote, Split, metrics

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ...models import HatFundamentalFiling
    from ..store import DataStore

logger = logging.getLogger(__name__)

CLOSE_KNOWN = time(15, 30)
PRICE_SERIES = ("EQ", "BE", "BZ")  # the first a session has wins


def known_at(public_at: datetime, fetched_at: datetime, point_in_time: bool) -> datetime:
    """When a stored filing's figures could be read as stored (module doc)."""
    if point_in_time:
        return as_utc(public_at)
    return max(as_utc(public_at), as_utc(fetched_at))


@dataclass(frozen=True)
class StoredFiling:
    id: str
    source: str
    basis: str
    period_end: date
    version: int
    known_at: datetime  # `known_at()`
    family: str
    point_in_time: bool = True

    @classmethod
    def of(cls, row: HatFundamentalFiling) -> StoredFiling:
        return cls(
            str(row.id),
            row.source,
            row.basis,
            row.period_end,
            row.version,
            known_at(row.public_at, row.fetched_at, row.point_in_time),
            row.family,
            row.point_in_time,
        )


@dataclass(frozen=True)
class MetricRow:
    as_of: datetime
    metric: Metric


def close_known_on(moment: datetime) -> date:
    """The last session whose close is known at `moment`: that day's from 15:30 IST, else the
    day before's (the caller finds the last session on or before it)."""
    local = moment.astimezone(IST)
    day = local.date()
    if local.time() >= CLOSE_KNOWN:
        return day
    return date.fromordinal(day.toordinal() - 1)


@dataclass
class Prices:
    """One instrument's closes by session, ascending, and its splits and bonuses (`book.Split`)."""

    days: list[date] = field(default_factory=list)
    closes: list[Decimal] = field(default_factory=list)
    splits: tuple[Split, ...] = ()

    def at(self, day: date) -> Quote | None:
        """The close of the last session on or before `day`."""
        index = bisect_right(self.days, day)
        if not index:
            return None
        return Quote(self.days[index - 1], self.closes[index - 1], self.splits)


def moments(filings: Sequence[StoredFiling], latest_session: date | None) -> list[datetime]:
    found = {as_utc(f.known_at) for f in filings}
    if latest_session is not None:
        found.add(as_utc(datetime.combine(latest_session, CLOSE_KNOWN, tzinfo=IST)))
    return sorted(found)


def metric_history(
    filings: Sequence[StoredFiling],
    facts: dict[str, list[KnownFact]],
    prices: Prices,
) -> list[MetricRow]:
    """The metric rows of one instrument (module doc): `facts` by filing id."""
    ordered = sorted(filings, key=lambda f: (as_utc(f.known_at), f.version))
    book = Book()
    last: dict[str, tuple[Decimal, date]] = {}
    out: list[MetricRow] = []
    i = 0
    for moment in moments(filings, prices.days[-1] if prices.days else None):
        while i < len(ordered) and as_utc(ordered[i].known_at) <= moment:
            filing = ordered[i]
            book.add(filing.source, filing.basis, filing.period_end, facts.get(filing.id, []))
            if filing.point_in_time:
                book.family = filing.family
            i += 1
        day = moment.astimezone(IST).date()
        for metric in metrics(book, day, prices.at(close_known_on(moment))):
            state = (metric.value, metric.period_end)
            if last.get(metric.name) != state:
                last[metric.name] = state
                out.append(MetricRow(moment, metric))
    return out


@dataclass
class DeriveReport:
    instruments: int = 0
    rows: int = 0
    metrics: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        top = ", ".join(f"{k}={v}" for k, v in sorted(self.metrics.items()))
        return f"instruments={self.instruments} rows={self.rows} by metric: {top}"


def load_prices(
    store: DataStore, instruments: set[str], since: date
) -> dict[str, Prices]:  # pragma: no cover - the price store
    import polars as pl

    from ..store import DATASET_BARS

    frames = []
    for day in store.days(DATASET_BARS):
        if day < since:
            continue
        for path in store.partition_files(DATASET_BARS, day):
            frame = pl.read_parquet(path, columns=["instrument_id", "series", "close"])
            frames.append(frame.with_columns(date=pl.lit(day)))
    if not frames:
        return {}
    bars = pl.concat(frames, how="vertical_relaxed").filter(
        pl.col("instrument_id").is_in(list(instruments)) & pl.col("series").is_in(PRICE_SERIES)
    )
    rank = {s: n for n, s in enumerate(PRICE_SERIES)}
    bars = (
        bars.with_columns(rank=pl.col("series").replace_strict(rank, default=99))
        .sort(["instrument_id", "date", "rank"])
        .unique(["instrument_id", "date"], keep="first", maintain_order=True)
    )
    out: dict[str, Prices] = defaultdict(Prices)
    for instrument_id, day, close in bars.select(["instrument_id", "date", "close"]).iter_rows():
        prices = out[instrument_id]
        prices.days.append(day)
        prices.closes.append(Decimal(close))
    return out


def load_splits(
    session: Session,
) -> dict[str, tuple[Split, ...]]:  # pragma: no cover
    """Every instrument's splits and bonuses (ex-date, price factor, known on), from the corporate
    actions (read only)."""
    from sqlalchemy import select

    from ...models import HatCorporateAction

    rows = session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.adjustment_factor,
            HatCorporateAction.known_on,
        ).where(HatCorporateAction.action_type.in_(("split", "bonus")))
    )
    out: dict[str, list[Split]] = defaultdict(list)
    for instrument_id, ex_date, factor, known_on in rows:
        out[str(instrument_id)].append((ex_date, Decimal(factor), known_on))
    return {k: tuple(sorted(v)) for k, v in out.items()}


METRIC_COLUMNS = (
    "instrument_id",
    "metric",
    "as_of",
    "symbol",
    "value",
    "period_end",
    "basis",
    "derived_at",
)


def derive(store: DataStore, price_since: date) -> DeriveReport:  # pragma: no cover - the database
    from sqlalchemy import select

    from app.database import db_session_scope, live_writer_engine

    from ...models import HatFundamentalFact, HatFundamentalFiling, HatFundamentalMetric
    from ...time import utc_now
    from .extract import copy_rows, raw_cursor

    HatFundamentalMetric.__table__.create(live_writer_engine, checkfirst=True)
    report = DeriveReport()
    now = utc_now()
    with db_session_scope(writes_data=True) as session:
        cursor = raw_cursor(session, HatFundamentalFiling)
        cursor.execute("LOCK TABLE hat_fundamental_metric IN EXCLUSIVE MODE")
        cursor.execute("LOCK TABLE hat_fundamental_filing IN SHARE MODE")
        by_instrument: dict[str, list[StoredFiling]] = defaultdict(list)
        symbols: dict[str, str] = {}
        for row in session.scalars(select(HatFundamentalFiling)):
            key = str(row.instrument_id)
            symbols[key] = row.symbol
            by_instrument[key].append(StoredFiling.of(row))
        prices = load_prices(store, set(by_instrument), price_since)
        for instrument_id, splits in load_splits(session).items():
            if instrument_id in prices:
                prices[instrument_id].splits = splits
        cursor.execute("TRUNCATE hat_fundamental_metric")
        for instrument_id, filings in by_instrument.items():
            facts = stored_facts(session, HatFundamentalFact, [f.id for f in filings])
            rows = metric_history(filings, facts, prices.get(instrument_id, Prices()))
            copy_rows(
                cursor,
                "hat_fundamental_metric",
                METRIC_COLUMNS,
                (
                    (
                        instrument_id,
                        r.metric.name,
                        r.as_of,
                        symbols[instrument_id],
                        r.metric.value,
                        r.metric.period_end,
                        r.metric.basis,
                        now,
                    )
                    for r in rows
                ),
            )
            report.instruments += 1
            report.rows += len(rows)
            for r in rows:
                report.metrics[r.metric.name] = report.metrics.get(r.metric.name, 0) + 1
            if report.instruments % 100 == 0:
                logger.info("derive %d/%d instruments", report.instruments, len(by_instrument))
    return report


def stored_facts(
    session: Session, model: type, filing_ids: Sequence[str]
) -> dict[str, list[KnownFact]]:  # pragma: no cover
    from sqlalchemy import select

    out: dict[str, list[KnownFact]] = defaultdict(list)
    rows = session.execute(
        select(model.filing_id, model.period_kind, model.period_end, model.item, model.value).where(
            model.filing_id.in_([uuid.UUID(i) for i in filing_ids]), model.item.is_not(None)
        )
    )
    for filing_id, kind, end, item, value in rows:
        out[str(filing_id)].append(KnownFact(kind, end, item, value))
    return out


# --- point-in-time reads ----------------------------------------------------------------------


def facts_as_of(
    session: Session, instrument_id: uuid.UUID, ts: datetime
) -> list[tuple[object, ...]]:  # pragma: no cover - the database
    """The instrument's facts known at `ts`: of filings known by `ts` (`known_at`), the latest
    version of each (source, basis, period end); every fact with its filing's columns."""
    from sqlalchemy import func, or_, select

    from ...models import HatFundamentalFact, HatFundamentalFiling

    filing = HatFundamentalFiling
    latest = (
        select(
            filing.id,
            func.row_number()
            .over(
                partition_by=(filing.source, filing.basis, filing.period_end),
                order_by=filing.version.desc(),
            )
            .label("rank"),
        )
        .where(
            filing.instrument_id == instrument_id,
            filing.public_at <= ts,
            or_(filing.point_in_time, filing.fetched_at <= ts),
        )
        .subquery()
    )
    rows = session.execute(
        select(filing, HatFundamentalFact)
        .join(latest, latest.c.id == filing.id)
        .join(HatFundamentalFact, HatFundamentalFact.filing_id == filing.id)
        .where(latest.c.rank == 1)
        .order_by(filing.period_end, filing.source, HatFundamentalFact.n)
    )
    return [tuple(r) for r in rows]


def metrics_as_of(
    session: Session, instrument_id: uuid.UUID, ts: datetime
) -> dict[str, tuple[Decimal, date]]:  # pragma: no cover - the database
    """Each metric's value and period end as known at `ts` (the latest row at or before it)."""
    from sqlalchemy import select

    from ...models import HatFundamentalMetric

    m = HatFundamentalMetric
    rows = session.execute(
        select(m.metric, m.value, m.period_end)
        .where(m.instrument_id == instrument_id, m.as_of <= ts)
        .order_by(m.metric, m.as_of)
    )
    return {name: (value, end) for name, value, end in rows}


def book_as_of(
    filings: Iterable[StoredFiling], facts: dict[str, list[KnownFact]], ts: datetime
) -> Book:
    """The book of the filings known at `ts` (the report's company sheet reads it)."""
    book = Book()
    for filing in sorted(filings, key=lambda f: (as_utc(f.known_at), f.version)):
        if as_utc(filing.known_at) <= ts:
            book.add(filing.source, filing.basis, filing.period_end, facts.get(filing.id, []))
            if filing.point_in_time:
                book.family = filing.family
    return book
