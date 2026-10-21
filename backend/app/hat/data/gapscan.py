"""The 09:16 full-market gap scan (HAT-DATA-120, 121, 122; HAT-BOT-052).

EP candidates cannot come from the nightly watchlist, because tomorrow's gappers are unknown
tonight. Just after the open, `cm_get_equity_stocks` is paged over the whole equity listing
(`page_equity_rows`), each EQ symbol's gap is `100 * (open / prev_close - 1)`, and the gappers of
at least 10% that pass the bot's universe criteria and can really gap that far are subscribed and
get their announcements attached.

"Can really gap that far": a 10% gap is impossible in a stock with a 2% or 5% price band, so a
candidate must have a 10% or 20% band, or be an F&O name. `[OWNER-PENDING]` F&O names are taken to
be the stocks with a known price-band row whose `band_pct` is null (NSE F&O stocks carry no
band); a stock with no price-band row at all is unknown and is never selected.

The scan runs as a job body under `run_job`, which writes the `hat_job_run` row with the start and
finish times; `run_gap_scan` then raises a health alert when the run finished at or after 09:20:00
IST, the first 5-minute bar (HAT-DATA-122).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils.market_hours import IST

from ..models import HatInstrument, HatJobRun, HatMarket
from ..ops.jobs import JobError, JobOutcome, JobResult, SessionFactory, run_job
from ..time import as_utc
from .instruments import current_band
from .mcp_client import McpClient, McpError
from .preopen import gap_pct, meets_gap
from .reference import page_equity_rows

__all__ = [
    "EP_BANDS",
    "EP_MIN_GAP_PCT",
    "GAP_SCAN_DEADLINE",
    "JOB_GAPSCAN",
    "Gapper",
    "GapRow",
    "gap_scan_body",
    "is_late",
    "run_gap_scan",
    "scan_rows",
    "select_gappers",
]

logger = logging.getLogger(__name__)

JOB_GAPSCAN = "gapscan"
EP_MIN_GAP_PCT = Decimal(10)
EP_BANDS = frozenset({Decimal(10), Decimal(20)})
GAP_SCAN_DEADLINE = time(9, 20)  # the first 5-minute bar closes
EQUITY_SERIES = "EQ"
CODE_SCAN_FAILED = "gapscan_mcp_failed"
ALERT_LATE = "gapscan_late"


@dataclass(frozen=True)
class GapRow:
    """One EQ symbol of the listing. `gap_pct` is None when a price is missing or not positive."""

    symbol: str
    open: Decimal | None
    prev_close: Decimal | None
    gap_pct: Decimal | None


@dataclass(frozen=True)
class Gapper:
    """A scan row with what the selection rule needs: universe result and price band."""

    row: GapRow
    passes_universe: bool
    band_known: bool
    band_pct: Decimal | None  # None with band_known means "no band": an F&O name


def _decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def scan_rows(client: McpClient) -> list[GapRow]:
    """Every EQ symbol of the listing exactly once, with its gap (HAT-DATA-120)."""
    rows = page_equity_rows(client)
    out = []
    for (symbol, series), raw in sorted(rows.items()):
        if series != EQUITY_SERIES:
            continue
        opened = _decimal(raw.get("openPrice"))
        prev = _decimal(raw.get("preClosePrice"))
        pct = gap_pct(opened, prev) if opened is not None and prev is not None else None
        out.append(GapRow(symbol, opened, prev, pct))
    return out


def is_candidate(item: Gapper, minimum_pct: Decimal = EP_MIN_GAP_PCT) -> bool:
    """The selection rule: gap >= 10, passes the universe, band 10 or 20 or F&O (no band)."""
    row = item.row
    if (
        row.open is None
        or row.prev_close is None
        or not meets_gap(row.open, row.prev_close, minimum_pct)
    ):
        return False
    if not item.passes_universe or not item.band_known:
        return False
    return item.band_pct is None or item.band_pct in EP_BANDS


def select_gappers(items: Iterable[Gapper], minimum_pct: Decimal = EP_MIN_GAP_PCT) -> list[str]:
    """Symbols of the items that are EP candidates, in input order (HAT-DATA-121)."""
    return [item.row.symbol for item in items if is_candidate(item, minimum_pct)]


def is_late(finished_at: datetime, trade_date: date) -> bool:
    """Whether a run finished at or after 09:20:00 IST on `trade_date`."""
    deadline = datetime.combine(trade_date, GAP_SCAN_DEADLINE, tzinfo=IST)
    return as_utc(finished_at) >= deadline


def _enrich(
    session: Session,
    rows: Sequence[GapRow],
    trade_date: date,
    universe: Callable[[HatInstrument], bool],
    minimum_pct: Decimal,
) -> list[Gapper]:
    """Attach instrument, universe result and price band, but only to rows that gapped enough."""
    market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
    if market_id is None:
        raise JobError("gapscan_no_market")
    by_symbol = {
        i.symbol: i
        for i in session.scalars(
            select(HatInstrument).where(
                HatInstrument.market_id == market_id,
                HatInstrument.active,
                HatInstrument.series == EQUITY_SERIES,
            )
        )
    }
    items = []
    for row in rows:
        instrument = by_symbol.get(row.symbol)
        gapped = (
            row.open is not None
            and row.prev_close is not None
            and meets_gap(row.open, row.prev_close, minimum_pct)
        )
        if instrument is None or not gapped:
            items.append(Gapper(row, False, False, None))
            continue
        found, band = current_band(session, instrument.id, trade_date)
        items.append(Gapper(row, universe(instrument), found, band))
    return items


def gap_scan_body(
    *,
    client: McpClient,
    session_factory: SessionFactory,
    trade_date: date,
    universe: Callable[[HatInstrument], bool],
    on_candidates: Callable[[Mapping[str, uuid.UUID]], None],
    minimum_pct: Decimal = EP_MIN_GAP_PCT,
) -> Callable[[], JobResult]:
    """A job body for `run_job`. Idempotent: the scan reads, then hands the candidates to
    `on_candidates` (subscribe at 09:17 and attach announcements), and a re-run subscribes the
    same symbols again, which the registry ignores."""

    def body() -> JobResult:
        calls_before = len(client.log.records)
        try:
            rows = scan_rows(client)
        except McpError as exc:
            raise JobError(CODE_SCAN_FAILED) from exc
        with session_factory() as session:
            items = _enrich(session, rows, trade_date, universe, minimum_pct)
            symbols = set(select_gappers(items, minimum_pct))
            ids = {
                i.symbol: i.id
                for i in session.scalars(
                    select(HatInstrument).where(
                        HatInstrument.symbol.in_(symbols), HatInstrument.series == EQUITY_SERIES
                    )
                )
                if i.active
            }
        on_candidates({s: ids[s] for s in sorted(symbols) if s in ids})
        return JobResult(calls=len(client.log.records) - calls_before, rows=len(ids))

    return body


def run_gap_scan(
    *,
    body: Callable[[], JobResult],
    trade_date: date,
    session_factory: SessionFactory,
    alert: Callable[[str], None],
) -> JobOutcome:
    """Run the scan once through `run_job` (which records `started_at` and `finished_at`), then
    alert when it finished at or after 09:20:00 IST."""
    outcome = run_job(JOB_GAPSCAN, trade_date, body, session_factory=session_factory)
    if outcome.executed:
        with session_factory() as session:
            row = session.scalar(
                select(HatJobRun).where(
                    HatJobRun.job == JOB_GAPSCAN, HatJobRun.trade_date == trade_date
                )
            )
            finished = None if row is None else row.finished_at
        if finished is not None and is_late(finished, trade_date):
            try:
                alert(ALERT_LATE)
            except Exception:
                logger.exception("gap scan late alert failed")
    return outcome
