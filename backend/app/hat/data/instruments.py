"""The instrument master, price bands and industries in the database (HAT-DATA-020, 025, 026);
the symbol history is `symbol_history`'s, the broker symbols `broker_symbols`'. Every function
writes in the caller's session and is idempotent: running it twice with the same input changes
nothing the second time.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..ids import uuid7
from ..models import (
    HatIndustry,
    HatInstrument,
    HatPriceBand,
)
from .corporate_actions import lock_reference_data
from .reference import BandRow, IndustryRow, Security, one_per_isin
from .symbol_history import (
    apply_symbol_change,
    close_history,
    held_by_another,
    open_interval,
    reopen_history,
)

logger = logging.getLogger(__name__)

CODE_LISTING_SHRUNK = "reference_listing_shrunk"
# A listing that drops more than half of at least this many active instruments is treated as a
# broken source, not as mass delisting.
SHRINK_GUARD_MIN_ACTIVE = 100


class ReferenceDataError(HatError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class UpsertReport:
    created: int = 0
    updated: int = 0
    renamed: int = 0
    delisted: int = 0
    reactivated: int = 0


CODE_ISIN_NOT_MERGED = "isin_not_merged"


def by_isin(instruments: Iterable[HatInstrument]) -> dict[str, HatInstrument]:
    """The instruments by ISIN. Two rows for one ISIN mean the database predates one id per ISIN
    (audit D5): `python -m app.hat.data.isin_merge up` has not run, so nothing may write."""
    out: dict[str, HatInstrument] = {}
    for instrument in instruments:
        if out.setdefault(instrument.isin, instrument) is not instrument:
            raise ReferenceDataError(CODE_ISIN_NOT_MERGED)
    return out


# --- instrument master ------------------------------------------------------------------------


def _create_instrument(
    session: Session, market_id: uuid.UUID, security: Security, today: date
) -> None:
    new_id = uuid7()
    session.add(
        HatInstrument(
            id=new_id,
            market_id=market_id,
            isin=security.isin,
            series=security.series,
            symbol=security.symbol,
            name=security.name,
            face_value=security.face_value,
            listed_on=security.listed_on,
            delisted_on=None,
            active=True,
        )
    )
    session.flush()
    # A symbol another instrument holds today (a new ISIN of a listed company: its listing date
    # is the company's) is taken from today; else from the listing date.
    start = security.listed_on or today
    if held_by_another(session, security.symbol, today):
        start = today
    open_interval(session, new_id, security.symbol, start)


def _refresh_instrument(
    session: Session, row: HatInstrument, security: Security, today: date, report: UpsertReport
) -> bool:
    """Bring one existing instrument in line with the listing; True when anything changed."""
    touched = False
    if row.series != security.series:
        # The ISIN moved to another series (EQ to BE, say): same instrument, new broker symbol.
        row.previous_series, row.series, row.series_since = row.series, security.series, today
        touched = True
    if row.symbol != security.symbol:
        apply_symbol_change(session, row.id, security.symbol, today)
        row.symbol = security.symbol
        report.renamed += 1
        touched = True
    for attr in ("name", "face_value", "listed_on"):
        value = getattr(security, attr)
        if (attr != "listed_on" or value is not None) and getattr(row, attr) != value:
            setattr(row, attr, value)
            touched = True
    if not row.active:
        row.active = True
        row.delisted_on = None
        reopen_history(session, row.id, security.symbol, today)
        report.reactivated += 1
        touched = True
    return touched


def upsert_instruments(
    session: Session,
    market_id: uuid.UUID,
    securities: Iterable[Security],
    *,
    today: date,
    complete: bool = True,
    still_listed: Collection[tuple[str, str]] = (),
) -> UpsertReport:
    """Bring `hat_instrument` in line with a listing. Instruments are keyed by (market, isin),
    so a rename or a series change keeps the id (a rename records a history row); an instrument
    missing from a `complete` listing is marked delisted, never deleted. `still_listed` holds
    (symbol, series) pairs the source lists but this listing has no ISIN row for: they are not
    delisted. An ISIN listed under two series keeps the best-ranked one (`one_per_isin`)."""
    lock_reference_data(session)  # the symbol history's writers, one at a time (`claim`)
    listing = {s.isin: s for s in one_per_isin(securities)}
    existing = by_isin(
        session.scalars(select(HatInstrument).where(HatInstrument.market_id == market_id))
    )
    report = UpsertReport()
    active_before = sum(1 for i in existing.values() if i.active)
    seen_active = sum(1 for key in listing if key in existing and existing[key].active)
    if complete and active_before >= SHRINK_GUARD_MIN_ACTIVE and seen_active * 2 < active_before:
        raise ReferenceDataError(CODE_LISTING_SHRUNK)
    for key, security in listing.items():
        row = existing.get(key)
        if row is None:
            _create_instrument(session, market_id, security, today)
            report.created += 1
        elif _refresh_instrument(session, row, security, today, report):
            report.updated += 1
    if complete:
        kept = set(still_listed)
        for key, row in existing.items():
            if key in listing or not row.active or (row.symbol, row.series) in kept:
                continue
            row.active = False
            row.delisted_on = today
            close_history(session, row.id, today)
            report.delisted += 1
    session.flush()
    return report


def register_history_instrument(
    session: Session,
    market_id: uuid.UUID,
    *,
    isin: str,
    series: str,
    symbol: str,
    first_seen: date,
) -> uuid.UUID:
    """The instrument of `isin`, created inactive under `series` when the master does not have
    it: a security seen only in old bhavcopy files (delisted, merged or renamed away) must still
    own its history (HAT-DATA-021). Name and face value are unknown here (the symbol and 0 stand
    in) until a weekly refresh lists it. Repeating the call, under any series, returns the same
    row."""
    lock_reference_data(session)
    row = session.scalar(
        select(HatInstrument).where(
            HatInstrument.market_id == market_id, HatInstrument.isin == isin
        )
    )
    if row is not None:
        return row.id
    new_id = uuid7()
    session.add(
        HatInstrument(
            id=new_id,
            market_id=market_id,
            isin=isin,
            series=series,
            symbol=symbol,
            name=symbol,
            face_value=Decimal(0),
            listed_on=first_seen,
            delisted_on=None,
            active=False,
        )
    )
    session.flush()
    open_interval(session, new_id, symbol, first_seen)  # the previous holder's ends at this bar
    session.flush()
    return new_id


# --- price bands and industries (HAT-DATA-025, 026) ---------------------------------------------


def _active_by_symbol_series(
    session: Session, market_id: uuid.UUID
) -> dict[tuple[str, str], HatInstrument]:
    return {
        (i.symbol, i.series): i
        for i in session.scalars(
            select(HatInstrument).where(HatInstrument.market_id == market_id, HatInstrument.active)
        )
    }


def current_band(
    session: Session, instrument_id: uuid.UUID, on: date
) -> tuple[bool, Decimal | None]:
    """(found, band_pct) of the row with the greatest effective_from <= `on`."""
    row = session.scalar(
        select(HatPriceBand)
        .where(HatPriceBand.instrument_id == instrument_id, HatPriceBand.effective_from <= on)
        .order_by(HatPriceBand.effective_from.desc())
        .limit(1)
    )
    if row is None:
        return False, None
    return True, None if row.band_pct is None else Decimal(row.band_pct)


def load_price_bands(
    session: Session, market_id: uuid.UUID, rows: Sequence[BandRow], effective: date
) -> tuple[int, int]:
    """Record bands that differ from the instrument's current one, from `effective`.
    Returns (rows written, rows for symbols with no active instrument)."""
    instruments = _active_by_symbol_series(session, market_id)
    written = unknown = 0
    for row in rows:
        instrument = instruments.get((row.symbol, row.series))
        if instrument is None:
            unknown += 1
            continue
        same_day = session.get(HatPriceBand, (instrument.id, effective))
        if same_day is not None:
            if same_day.band_pct != row.band_pct:
                same_day.band_pct = row.band_pct
                written += 1
            continue
        found, current = current_band(session, instrument.id, effective)
        if not found or current != row.band_pct:
            session.add(
                HatPriceBand(
                    instrument_id=instrument.id, band_pct=row.band_pct, effective_from=effective
                )
            )
            written += 1
    session.flush()
    return written, unknown


def current_industry(session: Session, instrument_id: uuid.UUID, on: date) -> HatIndustry | None:
    return session.scalar(
        select(HatIndustry)
        .where(HatIndustry.instrument_id == instrument_id, HatIndustry.effective_from <= on)
        .order_by(HatIndustry.effective_from.desc())
        .limit(1)
    )


def load_industries(
    session: Session, market_id: uuid.UUID, rows: Sequence[IndustryRow], effective: date
) -> tuple[int, int]:
    """Record industries that differ from the current one. Returns (written, unmatched)."""
    by_symbol: dict[str, HatInstrument] = {}
    for (symbol, series), instrument in sorted(
        _active_by_symbol_series(session, market_id).items()
    ):
        if series == "EQ" or symbol not in by_symbol:
            by_symbol[symbol] = instrument
    written = unmatched = 0
    for row in rows:
        instrument = by_symbol.get(row.symbol)
        if instrument is None:
            unmatched += 1
            continue
        same_day = session.get(HatIndustry, (instrument.id, effective))
        if same_day is not None:
            if (same_day.sector, same_day.industry) != (row.sector, row.industry):
                same_day.sector, same_day.industry = row.sector, row.industry
                written += 1
            continue
        current = current_industry(session, instrument.id, effective)
        if current is None or (current.sector, current.industry) != (row.sector, row.industry):
            session.add(
                HatIndustry(
                    instrument_id=instrument.id,
                    sector=row.sector,
                    industry=row.industry,
                    effective_from=effective,
                )
            )
            written += 1
    session.flush()
    return written, unmatched
