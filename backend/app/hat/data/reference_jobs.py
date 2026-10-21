"""Job bodies `reference.weekly`, `bands.nightly` and `calendar.refresh` (HAT-DATA-020..027).

Each body is meant for `run_job` and is idempotent: a crash after the body returns but before
the success is recorded re-runs it, and every write below changes nothing the second time. The
text of the NSE files comes from injected sources (a recorded file in tests, a download
elsewhere); nothing here opens a connection.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.symbol_master import SymbolMaster

from ..models import HatMarket
from ..ops.jobs import JobError, JobResult, SessionFactory
from . import calendar as hat_calendar
from . import instruments
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

logger = logging.getLogger(__name__)

JOB_REFERENCE_WEEKLY = "reference.weekly"
JOB_BANDS_NIGHTLY = "bands.nightly"
JOB_CALENDAR_REFRESH = "calendar.refresh"

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


def file_source(path: Path) -> TextSource:
    """A source reading a local file (a recorded fixture or a download saved by an operator)."""
    return lambda: path.read_text(encoding="utf-8")


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
) -> Callable[[], JobResult]:
    """Weekly: instrument master, symbol history, Fyers symbols and industries."""

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
            instruments.seed_symbol_history(session, market_id)
            brokers = instruments.refresh_fyers_symbols(session, master, market_id)
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
        return JobResult(calls=client.log.calls, rows=rows)

    return body


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
    """Yearly and on change: load holidays into `hat_trading_calendar` and re-install them."""

    def body() -> JobResult:
        try:
            days, _ = hat_calendar.parse_nse_holidays(_read(holiday_list, "calendar_unavailable"))
        except ValueError as exc:
            raise JobError("calendar_bad_response") from exc
        if not days:
            raise JobError("calendar_empty")
        with session_factory() as session:
            changed = hat_calendar.upsert_calendar(
                session, _market_id(session), days, replace_span=True
            )
            session.commit()
            hat_calendar.install_from_session(session)
        return JobResult(rows=changed)

    return body
