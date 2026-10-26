"""What the nightly job bodies take from outside themselves (`NightlyDeps`) and the lookups they
share: the next trading session and a configured MCP client."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.bhavcopy import Fetcher
from ..data.calendar import session_calendar
from ..data.kite_symbols import KiteSymbolData
from ..data.mcp_client import McpClient
from ..data.reference_jobs import CircuitSource, TextSource
from ..data.store import DataStore
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobError, SessionFactory


@dataclass(frozen=True)
class TextSources:
    """Where the reference files come from: NSE downloads in production, fixtures in checks."""

    equity_list: TextSource
    holidays: TextSource
    price_bands: Callable[[date], TextSource]
    industry_list: TextSource | None = None
    circuit_limits: CircuitSource | None = None
    symbol_changes: TextSource | None = None  # NSE symbolchange.csv: the ISIN lineage
    name_changes: TextSource | None = None
    # the `bc` CSV of NSE's PR archive of a trade date: the dividends `ca.adjust` stores (CA4)
    corporate_actions: Callable[[date], TextSource] | None = None
    # the NSE tokens and Kite's dump for the zerodha symbols, as of a date: set only while Kite
    # is the active broker (the weekly refresh then fills `hat_broker_symbol` for it)
    kite_symbols: Callable[[date], KiteSymbolData] | None = None


@dataclass
class NightlyDeps:
    store: DataStore
    session_factory: SessionFactory
    # the data jobs' sessions (ingest, corporate actions, quality, reference, bands, calendar):
    # the only ones that may write hat_live
    data_session_factory: SessionFactory
    clock: Clock
    user_id: uuid.UUID
    mcp: Callable[[], McpClient | None]  # a fresh client per run, so its call count is the run's
    bhavcopy: Callable[[], Fetcher]  # the 22:00 fallback's downloader
    sources: TextSources
    total_equity: Callable[[date], Decimal]  # the account book equity (Q1)
    # an account's last successful fill sync (`Desks.fills_read_at`): `ca.rescale`'s gate (Q8)
    fills_read_at: Callable[[uuid.UUID], datetime | None]
    sleep: Callable[[float], None]  # the ingest's wait; the runtime's raises when HAT stops
    notifier: Notifier = field(default_factory=LogNotifier)
    digest: Callable[[uuid.UUID, date], None] | None = None  # Reports.send_watchlist_digest
    on_loaded: Callable[[], None] = lambda: None  # new daily bars: drop the cached daily view


def next_session(session: Session, day: date) -> date | None:
    """The first trading day of the calendar after `day`; None when the calendar holds none."""
    calendar = session_calendar(session)
    return None if calendar is None else calendar.next_trading_day(day)


def mcp_client(deps: NightlyDeps) -> McpClient:
    client = deps.mcp()
    if client is None:
        raise JobError("mcp_not_configured")
    return client
