"""The broker symbols in the database (HAT-DATA-024): each active instrument's Fyers symbol, from
the Fyers symbol master, and its Kite tradingsymbol, from the Kite instrument list, in
`hat_broker_symbol`. A symbol that does not round-trip is skipped and reported; a refresh
replaces the broker's rows in the caller's transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..configs.records import BROKER_FYERS, BROKER_ZERODHA
from ..models import HatBrokerSymbol, HatInstrument
from ..time import utc_now
from .instruments import ReferenceDataError
from .kite_symbols import KiteListing, kite_symbol, plausible_refresh
from .symbols import SymbolMaster

logger = logging.getLogger(__name__)

# The series a Fyers symbol is written for; BZ, BL, IL and the rest get none.
FYERS_SERIES = frozenset({"EQ", "BE"})


@dataclass
class BrokerSymbolReport:
    written: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)  # instruments whose symbol does not round-trip


def fyers_symbol(master: SymbolMaster, instrument: HatInstrument) -> str | None:
    """The Fyers symbol via `SymbolMaster.to_fyers`, or None when it does not map back to the
    instrument's NSE symbol (never write a mapping that could route an order to another stock).
    It names the series the instrument trades under now: EQ as `-EQ` (what `to_fyers` emits), BE
    as `-BE`; any other series has none (no order can be routed to it)."""
    if instrument.series not in FYERS_SERIES:
        return None
    try:
        symbol = master.to_fyers(instrument.symbol)
    except ValueError:
        return None
    if not symbol.endswith("-EQ"):
        return None  # an index or other non-equity symbol must never stand for a stock
    if instrument.series == "BE":
        symbol = symbol.removesuffix("-EQ") + "-BE"
    if master.to_db(symbol) != instrument.symbol:
        return None
    return symbol


def _broker_instruments(session: Session, broker: str) -> list[tuple[str, str]]:
    rows = session.execute(
        select(HatInstrument.symbol, HatBrokerSymbol.broker_symbol)
        .join(HatBrokerSymbol, HatBrokerSymbol.instrument_id == HatInstrument.id)
        .where(HatBrokerSymbol.broker == broker, HatInstrument.active.is_(True))
        .order_by(HatInstrument.symbol, HatBrokerSymbol.broker_symbol)
    ).all()
    return [(symbol, broker_symbol) for symbol, broker_symbol in rows]


def fyers_instruments(session: Session) -> list[tuple[str, str]]:
    """(database symbol, Fyers symbol) of every active instrument that has a Fyers row, sorted
    by symbol: the instrument list HAT's broker adapter reports. It comes from `hat_instrument`
    and `hat_broker_symbol` only, so an index, a delisted stock or a symbol nobody mapped is never
    in it (HAT-DATA-024)."""
    return _broker_instruments(session, BROKER_FYERS)


def kite_instruments(session: Session) -> list[tuple[str, str]]:
    """(database symbol, Kite trading symbol) of every active instrument with a zerodha row, as
    `fyers_instruments` is for Fyers."""
    return _broker_instruments(session, BROKER_ZERODHA)


Mapped = tuple[str, int | None]  # (broker symbol, the broker's instrument token if it has one)


def _active_by_id(session: Session, market_id: uuid.UUID) -> list[HatInstrument]:
    instruments = session.scalars(
        select(HatInstrument).where(HatInstrument.market_id == market_id)
    ).all()
    return sorted((i for i in instruments if i.active), key=lambda i: i.id.bytes)


def _replace_broker_rows(
    session: Session, broker: str, wanted: Mapping[uuid.UUID, Mapped], report: BrokerSymbolReport
) -> BrokerSymbolReport:
    """Make `broker`'s rows exactly `wanted`. Delete first and flush, so a symbol that moves
    between instruments never trips the unique key."""
    rows = {
        r.instrument_id: r
        for r in session.scalars(select(HatBrokerSymbol).where(HatBrokerSymbol.broker == broker))
    }
    for instrument_id, row in rows.items():
        if wanted.get(instrument_id) != (row.broker_symbol, row.instrument_token):
            session.delete(row)
            report.removed += instrument_id not in wanted
    session.flush()
    now = utc_now()
    for instrument_id, (symbol, token) in wanted.items():
        row = rows.get(instrument_id)
        if row is None or (row.broker_symbol, row.instrument_token) != (symbol, token):
            session.add(
                HatBrokerSymbol(
                    instrument_id=instrument_id,
                    broker=broker,
                    broker_symbol=symbol,
                    instrument_token=token,
                    refreshed_at=now,
                )
            )
            report.written += 1
    session.flush()
    return report


def refresh_fyers_symbols(
    session: Session, master: SymbolMaster, market_id: uuid.UUID
) -> BrokerSymbolReport:
    """Make the Fyers rows exactly the mappings of active instruments. Idempotent."""
    report = BrokerSymbolReport()
    wanted: dict[uuid.UUID, Mapped] = {}
    taken: set[str] = set()
    for instrument in _active_by_id(session, market_id):
        symbol = fyers_symbol(master, instrument)
        if symbol is None or symbol in taken:
            report.skipped.append(instrument.symbol)
            continue
        taken.add(symbol)
        wanted[instrument.id] = (symbol, None)
    return _replace_broker_rows(session, BROKER_FYERS, wanted, report)


def refresh_kite_symbols(
    session: Session,
    market_id: uuid.UUID,
    nse_tokens: Mapping[tuple[str, str], int],
    listings: Mapping[int, KiteListing],
) -> BrokerSymbolReport:
    """Make the zerodha rows exactly the Kite listings of active instruments (`kite_symbol`:
    by ISIN and series to NSE's exchange token, then Kite's row with that token, its trading
    symbol checked), each with Kite's `instrument_token`. Idempotent. An empty dump or token
    list is refused (`kite_symbols_empty`), and so is a result that maps no instrument or far
    fewer than are stored (`kite_symbols_shrank`, `plausible_refresh`): a bad input never
    empties or guts the rows, the last set stays and the caller alerts."""
    if not nse_tokens or not listings:
        raise ReferenceDataError("kite_symbols_empty")
    report = BrokerSymbolReport()
    wanted: dict[uuid.UUID, Mapped] = {}
    taken: set[str] = set()
    for instrument in _active_by_id(session, market_id):
        listing = kite_symbol(
            instrument.isin, instrument.series, instrument.symbol, nse_tokens, listings
        )
        if listing is None or listing.tradingsymbol in taken:
            report.skipped.append(instrument.symbol)
            continue
        taken.add(listing.tradingsymbol)
        wanted[instrument.id] = (listing.tradingsymbol, listing.instrument_token)
    stored = session.scalar(select(func.count()).where(HatBrokerSymbol.broker == BROKER_ZERODHA))
    if not plausible_refresh(stored or 0, len(wanted)):
        logger.warning("kite symbols: %d mapped of %d stored; kept", len(wanted), stored)
        raise ReferenceDataError("kite_symbols_shrank")
    return _replace_broker_rows(session, BROKER_ZERODHA, wanted, report)
