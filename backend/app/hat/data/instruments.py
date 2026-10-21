"""Instrument master, symbol history, broker symbols, price bands and industries in the
database (HAT-DATA-020..026). Every function writes in the caller's session and is idempotent:
running it twice with the same input changes nothing the second time.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.symbol_history import SymbolChangeType, SymbolHistory
from app.services.symbol_master import SymbolMaster

from ..errors import HatError
from ..ids import uuid7
from ..models import (
    HatBrokerSymbol,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatPriceBand,
)
from ..time import utc_now
from .reference import BandRow, IndustryRow, Security

logger = logging.getLogger(__name__)

EPOCH = date(1900, 1, 1)
CODE_LISTING_SHRUNK = "reference_listing_shrunk"
# A listing that drops more than half of at least this many active instruments is treated as a
# broken source, not as mass delisting.
SHRINK_GUARD_MIN_ACTIVE = 100
BROKER_FYERS = "fyers"


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


# --- symbol history ---------------------------------------------------------------------------


def _history(session: Session, instrument_id: uuid.UUID) -> list[HatInstrumentSymbolHistory]:
    return list(
        session.scalars(
            select(HatInstrumentSymbolHistory)
            .where(HatInstrumentSymbolHistory.instrument_id == instrument_id)
            .order_by(HatInstrumentSymbolHistory.valid_from)
        )
    )


def _new_interval(
    instrument_id: uuid.UUID, symbol: str, start: date, end: date | None
) -> HatInstrumentSymbolHistory:
    return HatInstrumentSymbolHistory(
        instrument_id=instrument_id, symbol=symbol, valid_from=start, valid_to=end
    )


def apply_symbol_change(
    session: Session,
    instrument_id: uuid.UUID,
    new_symbol: str,
    effective: date,
    *,
    old_symbol: str | None = None,
) -> bool:
    """Make `new_symbol` the instrument's symbol from `effective`, keeping the validity
    intervals of its rows disjoint. `old_symbol`, when known (the legacy history), labels the
    interval before `effective`. Returns True when any row changed.
    """
    rows = _history(session, instrument_id)
    changed = False
    exact = next((r for r in rows if r.valid_from == effective), None)
    covering = next(
        (
            r
            for r in reversed(rows)
            if r.valid_from < effective and (r.valid_to is None or r.valid_to > effective)
        ),
        None,
    )
    if exact is not None:
        if exact.symbol != new_symbol:
            exact.symbol = new_symbol
            changed = True
    elif covering is not None:
        if old_symbol is not None and covering.symbol == new_symbol:
            covering.symbol = old_symbol  # the legacy record says what it was before
        if covering.symbol != new_symbol:
            session.add(_new_interval(instrument_id, new_symbol, effective, covering.valid_to))
            covering.valid_to = effective
            changed = True
    else:
        # No row covers `effective` (before the first row, or in a gap after a delisting): the
        # new symbol holds until the next row begins.
        following = next((r for r in rows if r.valid_from > effective), None)
        end = following.valid_from if following is not None else None
        session.add(_new_interval(instrument_id, new_symbol, effective, end))
        changed = True
    session.flush()
    if old_symbol is not None and not any(
        r.valid_from < effective for r in _history(session, instrument_id)
    ):
        session.add(_new_interval(instrument_id, old_symbol, EPOCH, effective))
        changed = True
    session.flush()
    return changed


def resolve_symbol(
    session: Session, market_id: uuid.UUID, symbol: str, on: date
) -> HatInstrument | None:
    """The one instrument whose symbol history covers (symbol, on). If several series share
    the symbol, the active one wins, then EQ, then the lowest id: at most one is returned."""
    candidates = session.scalars(
        select(HatInstrument)
        .join(
            HatInstrumentSymbolHistory,
            HatInstrumentSymbolHistory.instrument_id == HatInstrument.id,
        )
        .where(
            HatInstrument.market_id == market_id,
            HatInstrumentSymbolHistory.symbol == symbol,
            HatInstrumentSymbolHistory.valid_from <= on,
            (HatInstrumentSymbolHistory.valid_to.is_(None))
            | (HatInstrumentSymbolHistory.valid_to > on),
        )
    ).all()
    if not candidates:
        return None
    return min(candidates, key=lambda i: (not i.active, i.series != "EQ", i.id.bytes))


def seed_symbol_history(session: Session, market_id: uuid.UUID) -> int:
    """Seed `hat_instrument_symbol_history` from the legacy `symbol_history` renames.

    Each name or symbol change whose new symbol is a current instrument (matched by ISIN when
    the legacy row has one, else by the new symbol) becomes a change at its effective date.
    Idempotent. Returns how many legacy renames matched no instrument.
    """
    renames = {SymbolChangeType.NAME_CHANGE, SymbolChangeType.SYMBOL_CHANGE}
    legacy = session.scalars(
        select(SymbolHistory)
        .where(SymbolHistory.change_type.in_(renames), SymbolHistory.new_symbol.is_not(None))
        .order_by(SymbolHistory.effective_from, SymbolHistory.id)
    ).all()
    unmatched = 0
    for record in legacy:
        if record.old_symbol == record.new_symbol:
            continue
        instrument = None
        if record.isin:
            instrument = session.scalar(
                select(HatInstrument)
                .where(HatInstrument.market_id == market_id, HatInstrument.isin == record.isin)
                .order_by(HatInstrument.series != "EQ", HatInstrument.id)
            )
        if instrument is None:
            instrument = resolve_symbol(session, market_id, record.new_symbol, date.max)
        if instrument is None:
            unmatched += 1
            continue
        apply_symbol_change(
            session,
            instrument.id,
            record.new_symbol,
            record.effective_from,
            old_symbol=record.old_symbol,
        )
    return unmatched


# --- instrument master ------------------------------------------------------------------------


def _close_history(session: Session, instrument_id: uuid.UUID, on: date) -> None:
    rows = _history(session, instrument_id)
    if rows and rows[-1].valid_to is None and rows[-1].valid_from < on:
        rows[-1].valid_to = on


def _reopen_history(session: Session, instrument_id: uuid.UUID, symbol: str, today: date) -> None:
    """A relisted instrument holds `symbol` again from `today`; the gap while it was delisted
    stays unclaimed, so a symbol another company used meanwhile still resolves to it."""
    rows = _history(session, instrument_id)
    if not rows or rows[-1].valid_to is None:
        return
    if rows[-1].valid_from == today:
        rows[-1].valid_to = None
        rows[-1].symbol = symbol
    else:
        session.add(_new_interval(instrument_id, symbol, today, None))


def upsert_instruments(
    session: Session,
    market_id: uuid.UUID,
    securities: Iterable[Security],
    *,
    today: date,
    complete: bool = True,
) -> UpsertReport:
    """Bring `hat_instrument` in line with a listing. Instruments are keyed by (market, isin,
    series), so a rename keeps the id and records a history row; an instrument missing from a
    `complete` listing is marked delisted, never deleted."""
    listing = {s.key: s for s in securities}
    existing = {
        (i.isin, i.series): i
        for i in session.scalars(select(HatInstrument).where(HatInstrument.market_id == market_id))
    }
    report = UpsertReport()
    active_before = sum(1 for i in existing.values() if i.active)
    seen_active = sum(1 for key in listing if key in existing and existing[key].active)
    if complete and active_before >= SHRINK_GUARD_MIN_ACTIVE and seen_active * 2 < active_before:
        raise ReferenceDataError(CODE_LISTING_SHRUNK)
    for key, security in listing.items():
        row = existing.get(key)
        if row is None:
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
            session.add(_new_interval(new_id, security.symbol, security.listed_on or today, None))
            report.created += 1
            continue
        touched = False
        if row.symbol != security.symbol:
            apply_symbol_change(session, row.id, security.symbol, today)
            row.symbol = security.symbol
            report.renamed += 1
            touched = True
        for attr in ("name", "face_value", "listed_on"):
            value = getattr(security, attr)
            if attr == "listed_on" and value is None:
                continue
            if getattr(row, attr) != value:
                setattr(row, attr, value)
                touched = True
        if not row.active:
            row.active = True
            row.delisted_on = None
            _reopen_history(session, row.id, security.symbol, today)
            report.reactivated += 1
            touched = True
        report.updated += touched
    if complete:
        for key, row in existing.items():
            if key not in listing and row.active:
                row.active = False
                row.delisted_on = today
                _close_history(session, row.id, today)
                report.delisted += 1
    session.flush()
    return report


# --- broker symbols (HAT-DATA-024) -------------------------------------------------------------


@dataclass
class BrokerSymbolReport:
    written: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)  # instruments whose symbol does not round-trip


def fyers_symbol(master: SymbolMaster, instrument: HatInstrument) -> str | None:
    """The Fyers symbol via `SymbolMaster.to_fyers`, or None when it does not map back to the
    instrument's NSE symbol (never write a mapping that could route an order to another stock).
    BE series use the `-BE` suffix Fyers expects (`to_fyers` always emits `-EQ`)."""
    try:
        symbol = master.to_fyers(instrument.symbol)
    except ValueError:
        return None
    if instrument.series == "BE":
        symbol = symbol.removesuffix("-EQ") + "-BE"
    if master.to_db(symbol) != instrument.symbol:
        return None
    return symbol


def refresh_fyers_symbols(
    session: Session, master: SymbolMaster, market_id: uuid.UUID
) -> BrokerSymbolReport:
    """Make the Fyers rows exactly the mappings of active instruments. Idempotent."""
    report = BrokerSymbolReport()
    instruments = session.scalars(
        select(HatInstrument).where(HatInstrument.market_id == market_id)
    ).all()
    wanted: dict[uuid.UUID, str] = {}
    taken: set[str] = set()
    for instrument in sorted((i for i in instruments if i.active), key=lambda i: i.id.bytes):
        symbol = fyers_symbol(master, instrument)
        if symbol is None or symbol in taken:
            report.skipped.append(instrument.symbol)
            continue
        taken.add(symbol)
        wanted[instrument.id] = symbol
    rows = {
        r.instrument_id: r
        for r in session.scalars(
            select(HatBrokerSymbol).where(HatBrokerSymbol.broker == BROKER_FYERS)
        )
    }
    # Delete first and flush, so a symbol that moves between instruments never trips the unique key.
    for instrument_id, row in rows.items():
        if wanted.get(instrument_id) != row.broker_symbol:
            session.delete(row)
            report.removed += instrument_id not in wanted
    session.flush()
    now = utc_now()
    for instrument_id, symbol in wanted.items():
        row = rows.get(instrument_id)
        if row is None or row.broker_symbol != symbol:
            session.add(
                HatBrokerSymbol(
                    instrument_id=instrument_id,
                    broker=BROKER_FYERS,
                    broker_symbol=symbol,
                    refreshed_at=now,
                )
            )
            report.written += 1
    session.flush()
    return report


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


def instruments_without_industry(session: Session, market_id: uuid.UUID, on: date) -> list[str]:
    """Active instruments with no industry row on `on`: reported, never given a shared one."""
    return sorted(
        instrument.symbol
        for instrument in _active_by_symbol_series(session, market_id).values()
        if current_industry(session, instrument.id, on) is None
    )
