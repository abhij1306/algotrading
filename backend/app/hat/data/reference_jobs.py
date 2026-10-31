"""Job bodies `reference.weekly`, `listing.refresh`, `bands.nightly` and `calendar.refresh`
(HAT-DATA-020..027).

Each body is meant for `run_job` and is idempotent: a crash after the body returns but before
the success is recorded re-runs it, and every write below changes nothing the second time. The
text of the NSE files comes from injected sources (a recorded file in tests, a download
elsewhere); nothing here opens a connection.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument, HatMarket
from ..ops.jobs import JobError, JobResult, SessionFactory
from . import broker_symbols, instruments, symbol_history
from . import calendar as hat_calendar
from .corporate_actions import lock_reference_data
from .isin import ordinary_equity
from .kite_symbols import KiteSymbolData
from .mcp_client import McpClient, McpError
from .reference import (
    BandRow,
    band_from_circuit_limits,
    build_listing_from_csv,
    build_listing_from_mcp,
    page_listing,
    parse_equity_list_csv,
    parse_industry_csv,
    parse_price_band_csv,
)
from .symbols import SymbolMaster, symbol_master

logger = logging.getLogger(__name__)

TextSource = Callable[[], str]


@dataclass(frozen=True)
class CircuitQuote:
    """A broker quote's circuit limits, the price-band fallback."""

    symbol: str
    series: str
    prev_close: Decimal
    upper: Decimal
    lower: Decimal


CircuitSource = Callable[[], Sequence[CircuitQuote]]


def _market_id(session: Session) -> object:
    market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
    if market_id is None:
        raise JobError("reference_no_market")
    return market_id


def _read(source: TextSource, code: str) -> str:
    try:
        return source()
    except Exception as exc:  # a source failure becomes a stable code; its text is not kept
        raise JobError(code) from exc


def reference_weekly(
    *,
    client: McpClient,
    equity_list: TextSource,
    industry_list: TextSource | None,
    session_factory: SessionFactory,
    master: SymbolMaster,
    today: date,
    renames: Callable[[], Iterable[symbol_history.SymbolRename]] | None = None,
    kite_symbols: Callable[[], KiteSymbolData] | None = None,
) -> Callable[[], JobResult]:
    """Weekly: instrument master, symbol history, Fyers symbols and industries; and the Kite
    symbols when `kite_symbols` is given (Kite is the active broker).

    `renames` is an optional source of recorded symbol renames (HAT reads no legacy table). The
    Kite symbols are refreshed in a transaction of their own after the rest is committed, so a
    failed Kite dump or bhavcopy (`kite_symbols_unavailable`) fails the job without losing the
    instrument master, and keeps the zerodha rows as they were."""

    def body() -> JobResult:
        csv_rows = parse_equity_list_csv(_read(equity_list, "reference_csv_unavailable"))
        if not csv_rows:
            raise JobError("reference_csv_empty")
        try:
            build = build_listing_from_mcp(page_listing(client), csv_rows)
        except McpError:
            logger.warning("mcp listing failed; using the equity list csv")
            build = build_listing_from_csv(csv_rows)
        with session_factory() as session:
            market_id = _market_id(session)
            report = instruments.upsert_instruments(
                session,
                market_id,
                build.securities,
                today=today,
                still_listed={(u.symbol, u.series) for u in build.unresolved},
            )
            if renames is not None:
                symbol_history.seed_symbol_history(session, market_id, renames())
            brokers = broker_symbols.refresh_fyers_symbols(session, master, market_id)
            rows = report.created + report.updated + report.delisted + brokers.written
            if industry_list is not None:
                try:
                    parsed = parse_industry_csv(
                        _read(industry_list, "reference_industry_unavailable")
                    )
                except JobError:
                    parsed = []
                written, _ = instruments.load_industries(session, market_id, parsed, today)
                rows += written
            session.commit()
        if kite_symbols is not None:
            rows += refresh_kite(session_factory, kite_symbols)
        return JobResult(calls=client.log.calls, rows=rows)

    return body


LISTING_REFRESH = "listing.refresh"


def listing_refresh(
    *,
    equity_list: TextSource,
    session_factory: SessionFactory,
    today: date,
    master: SymbolMaster = symbol_master,
) -> Callable[[], JobResult]:
    """Daily, in the data update: the stocks of the instrument master in line with NSE's equity
    list (`EQUITY_L.csv`) alone, so an ISIN the bars step registered (inactive, no name, face
    value 0) is activated and named the session it lists, a listed stock's rename or series move
    is taken, and a stock the list no longer holds is marked delisted. The list holds stocks
    only: every active instrument that is not a stock (`isin.ordinary_equity`: ETFs and other
    funds, rights entitlements) is kept as listed (`still_listed`), never delisted here;
    `reference.weekly` owns the rest. The shrink guard (`reference_listing_shrunk`) refuses a
    list that lost half the master. The Fyers symbols follow in the same transaction
    (`broker_symbols.refresh_fyers_symbols`, no download), so a rename or series move taken here
    never leaves an order routed to the old symbol until the next `reference.weekly`."""

    def body() -> JobResult:
        csv_rows = parse_equity_list_csv(_read(equity_list, "reference_csv_unavailable"))
        stocks = [s for s in csv_rows if ordinary_equity(s.isin)]
        if not stocks:  # a list of no stock would delist every one
            raise JobError("reference_csv_empty")
        build = build_listing_from_csv(stocks)
        with session_factory() as session:
            market_id = _market_id(session)
            lock_reference_data(session)  # before the read: no writer activates one meanwhile
            others = {
                (i.symbol, i.series)
                for i in session.scalars(
                    select(HatInstrument).where(
                        HatInstrument.market_id == market_id, HatInstrument.active.is_(True)
                    )
                )
                if not ordinary_equity(i.isin)
            }
            report = instruments.upsert_instruments(
                session, market_id, build.securities, today=today, still_listed=others
            )
            brokers = broker_symbols.refresh_fyers_symbols(session, master, market_id)
            session.commit()
        logger.info(
            "listing refresh: %s created, %s updated (%s reactivated, %s renamed), %s delisted;"
            " fyers symbols %s written, %s removed",
            report.created,
            report.updated,
            report.reactivated,
            report.renamed,
            report.delisted,
            brokers.written,
            brokers.removed,
        )
        return JobResult(rows=report.created + report.updated + report.delisted + brokers.written)

    return body


def refresh_kite(session_factory: SessionFactory, source: Callable[[], KiteSymbolData]) -> int:
    """Refresh the zerodha broker symbols from `source` in a transaction of its own; the rows
    written. A source that fails, or maps nothing, raises `JobError` and changes no row."""
    try:
        nse_tokens, listings = source()
    except Exception as exc:  # a download failure becomes a stable code; its text is not kept
        raise JobError("kite_symbols_unavailable") from exc
    with session_factory() as session:
        try:
            report = broker_symbols.refresh_kite_symbols(
                session, _market_id(session), nse_tokens, listings
            )
        except instruments.ReferenceDataError as exc:
            raise JobError(exc.code) from exc
        session.commit()
    logger.info("kite symbols: %d written, %d skipped", report.written, len(report.skipped))
    return report.written


def bands_nightly(
    *,
    price_band_file: TextSource,
    circuit_limits: CircuitSource | None,
    session_factory: SessionFactory,
    effective: date,
) -> Callable[[], JobResult]:
    """Nightly: price bands from the NSE file, else from broker circuit limits."""

    def body() -> JobResult:
        rows: list[BandRow] = []
        try:
            rows, _ = parse_price_band_csv(price_band_file())
        except Exception:
            logger.warning("price band file unavailable")
        if not rows and circuit_limits is not None:
            for quote in circuit_limits():
                band = band_from_circuit_limits(quote.prev_close, quote.upper, quote.lower)
                if band is not None:
                    rows.append(BandRow(quote.symbol, quote.series, band))
        if not rows:
            raise JobError("bands_unavailable")
        with session_factory() as session:
            written, _ = instruments.load_price_bands(session, _market_id(session), rows, effective)
            session.commit()
        return JobResult(rows=written)

    return body


def calendar_refresh(
    *, holiday_list: TextSource, session_factory: SessionFactory
) -> Callable[[], JobResult]:
    """Yearly and on change: load holidays into `hat_trading_calendar`, with the recorded special
    sessions of its years the list leaves out (`calendar.with_recorded_sessions`) and a row for
    every other weekday of the years the list covers (`calendar.with_weekdays`), plus the
    recorded past years the live list does not cover (`calendar.store_recorded_history`)."""

    def body() -> JobResult:
        try:
            days, _ = hat_calendar.parse_nse_holidays(_read(holiday_list, "calendar_unavailable"))
        except ValueError as exc:
            raise JobError("calendar_bad_response") from exc
        if not days:
            raise JobError("calendar_empty")
        with session_factory() as session:
            market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
            if market is None:
                raise JobError("reference_no_market")
            full = hat_calendar.with_weekdays(
                hat_calendar.with_recorded_sessions(days),
                market.session_open,
                market.session_close,
            )
            changed = hat_calendar.upsert_calendar(session, market.id, full, replace_span=True)
            changed += hat_calendar.store_recorded_history(
                session, market, skip_years={d.day.year for d in full}
            )
            session.commit()
        return JobResult(rows=changed)

    return body
