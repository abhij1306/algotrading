"""Fakes for the verification suites: an RSS renderer, a priced cm-market listing server
and a database holding instruments and announcements. No network anywhere."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from html import escape
from typing import Any
from uuid import UUID

from hat_mcp_support import REFERENCE_TABLES, seed_market
from hat_support import memory_engine
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.hat.data.mcp_client import McpTransportError
from app.hat.models import HatAnnouncement, HatInstrument, HatInstrumentSymbolHistory

LIVE_TABLES = (*REFERENCE_TABLES, HatAnnouncement)
EPOCH = date(2000, 1, 1)


def live_engine() -> Engine:
    return memory_engine(*LIVE_TABLES)


def add_instrument(
    session: Session, market_id: UUID, symbol: str, isin: str, *, series: str = "EQ"
) -> HatInstrument:
    """An active instrument with its symbol history, as the weekly refresh leaves it."""
    instrument = HatInstrument(
        market_id=market_id,
        isin=isin,
        series=series,
        symbol=symbol,
        name=f"{symbol} Ltd",
        face_value=Decimal(10),
        active=True,
    )
    session.add(instrument)
    session.flush()
    session.add(
        HatInstrumentSymbolHistory(
            instrument_id=instrument.id, symbol=symbol, valid_from=EPOCH, valid_to=None
        )
    )
    session.flush()
    return instrument


def seeded_session(engine: Engine) -> tuple[Session, UUID]:
    session = Session(engine)
    return session, seed_market(session)


def rss_item(
    guid: str | None,
    title: str,
    published: datetime,
    *,
    symbol: str | None = None,
    isin: str | None = None,
    url: str = "https://nsearchives.example/x.pdf",
    category: str | None = None,
    company: str | None = None,
) -> str:
    parts = [f"<title>{escape(title)}</title>", f"<link>{escape(url)}</link>"]
    parts.append(f"<pubDate>{published.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>")
    if guid is not None:
        parts.append(f'<guid isPermaLink="false">{escape(guid)}</guid>')
    if symbol:
        parts.append(f"<symbol>{escape(symbol)}</symbol>")
    if isin:
        parts.append(f"<isin>{escape(isin)}</isin>")
    if category:
        parts.append(f"<category>{escape(category)}</category>")
    if company:
        parts.append(f"<company>{escape(company)}</company>")
    return "<item>" + "".join(parts) + "</item>"


def rss_document(items: Sequence[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>'
        + "".join(items)
        + "</channel></rss>"
    )


class PricedListingServer:
    """A fake cm-market `cm_get_equity_stocks` whose rows carry prices, filtered by prefix and cut
    at `limit` (the real cap is 500)."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        fail: bool = False,
    ) -> None:
        self.rows = sorted(rows, key=lambda r: (r["symbol"], r["series"]))
        self.fail = fail
        self.requests: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object:
        if self.fail:
            raise McpTransportError()
        prefix, limit = arguments["symbolFilter"], min(int(arguments["limit"]), 500)
        with self._lock:
            self.requests.append((prefix, limit))
        rows = [dict(r) for r in self.rows if r["symbol"].startswith(prefix)][:limit]
        return {"segment": "EQUITY", "returned": len(rows), "stocks": rows}
