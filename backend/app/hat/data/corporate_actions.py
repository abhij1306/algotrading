"""Corporate actions: parsing, storage in `hat_corporate_action` and the factors the adjusted view
uses (HAT-DATA-052, 053, 054, 055, 094; HAT-DEC-B27).

A factor multiplies prices before the ex-date (a 1:2 split is 0.5). Sources, in the order they
win on a conflict: the MCP's structured `adjustmentFactor`, then NSE's archive CSV text parsed
with HAT's own text helpers (`app.hat.data.ratios`). A stored action is never
changed by a later load: a different factor for a stored key is reported as a conflict.

An MCP action that changes no price (factor 1: a dividend, an AGM) is stored too, as
`dividend` or `notice` (PR 47, HAT-DATA-052): Fyers cancels a resting GTT before any ex-date,
so the morning GTT check needs every ex-date of a held name (HAT-EXEC-145). Such a row is never
a price factor (`stored_factors`) and never an action the rescale handles or flags.

`lookup_actions` is the one MCP lookup, pooled on the client: the nightly ingest, the next-session
and quarantine lookups and the history build all read actions through it, so an MCP page means
the same everywhere (a failed, incomplete or unreadable page fails its symbol).
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data.ratios import find_bonus_ratio, split_pair_numbers

from ..models import HatCorporateAction
from ..money import exact
from .mcp_client import McpClient, McpError
from .store import ActionFactor

logger = logging.getLogger(__name__)

SOURCE_MCP = "mcp"
SOURCE_ARCHIVE = "archive"
TYPE_SPLIT = "split"
TYPE_BONUS = "bonus"
TYPE_OTHER = "other"
TYPE_DIVIDEND = "dividend"
TYPE_NOTICE = "notice"  # any other action the MCP gives factor 1: no price changes
PRICE_NEUTRAL_TYPES = (TYPE_DIVIDEND, TYPE_NOTICE)
STORED_PLACES = 12  # a factor such as 2/3 is stored rounded to 12 places

_MCP_TYPES = {"SPLIT": TYPE_SPLIT, "BONUS": TYPE_BONUS}


@dataclass(frozen=True)
class ParsedAction:
    action_type: str
    factor: Fraction


def parse_action_text(text: str) -> ParsedAction | None:
    """A face-value split from a to b gives b / a; a bonus of x new for y held gives
    y / (x + y). Anything else, or a factor outside (0, 1), is None: never a guessed factor."""
    lowered = text.lower()
    if "split" in lowered or "sub-division" in lowered or "sub division" in lowered:
        pair = split_pair_numbers(lowered)
        if pair is not None:
            old, new = pair
            factor = new / old
            if 0 < factor < 1:
                return ParsedAction(TYPE_SPLIT, factor)
        return None
    ratio = find_bonus_ratio(lowered)
    if ratio is not None:
        new_shares, held = ratio
        if new_shares > 0 and held > 0:
            return ParsedAction(TYPE_BONUS, Fraction(held, new_shares + held))
    return None


def factor_to_decimal(factor: Fraction) -> Decimal:
    """The stored form of an exact factor."""
    value = Decimal(factor.numerator) / Decimal(factor.denominator)
    return value.quantize(Decimal(1).scaleb(-STORED_PLACES), ROUND_HALF_EVEN)


@dataclass(frozen=True)
class ActionRecord:
    instrument_id: object
    ex_date: date
    action_type: str
    factor: Decimal
    raw_text: str
    source: str

    @property
    def key(self) -> tuple[object, date, str]:
        return (self.instrument_id, self.ex_date, self.action_type)


@dataclass
class Normalised:
    records: list[ActionRecord] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)  # price-affecting text with no factor
    ignored: int = 0  # archive dividends and other texts that do not change prices


def _iso(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def from_mcp(instrument_id: object, rows: Iterable[dict]) -> Normalised:
    """`get_corporate_actions` rows of one instrument. Splits and bonuses keep the MCP factor;
    other types with a factor other than 1 are stored as `other`; an action of factor 1 is
    stored price-neutral, as `dividend` or `notice` (its ex-date is what HAT needs)."""
    out = Normalised()
    for row in rows:
        ex = _iso(row.get("exDate"))
        raw_factor = row.get("adjustmentFactor")
        text = str(row.get("purpose") or "")
        kind = _MCP_TYPES.get(str(row.get("actionType") or "").upper(), TYPE_OTHER)
        factor = exact(raw_factor, floats=True, positive=True)
        if ex is None or factor is None:
            out.unparsed.append(text)
            continue
        if factor == 1:
            raw_type = str(row.get("actionType") or "").upper()
            kind = TYPE_DIVIDEND if raw_type == "DIVIDEND" else TYPE_NOTICE
        out.records.append(ActionRecord(instrument_id, ex, kind, factor, text, SOURCE_MCP))
    return out


_ARCHIVE_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _archive_date(raw: str) -> date | None:
    parts = raw.strip().lower().replace("/", "-").split("-")
    if len(parts) == 3 and parts[1][:3] in _ARCHIVE_MONTHS:
        try:
            return date(int(parts[2]), _ARCHIVE_MONTHS.index(parts[1][:3]) + 1, int(parts[0]))
        except ValueError:
            return None
    return _iso(raw)


def from_archive_csv(text: str, instrument_for: Callable[[str, date], object | None]) -> Normalised:
    """NSE's corporate-action archive CSV (columns SYMBOL, PURPOSE, EX-DATE). The symbol is
    resolved to an instrument as of the ex-date, so a renamed company still matches."""
    out = Normalised()
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()}
        purpose = row.get("PURPOSE", "")
        ex = _archive_date(row.get("EX-DATE", ""))
        parsed = parse_action_text(purpose)
        if parsed is None:
            lowered = purpose.lower()
            # a consolidation changes prices too, and HAT never guesses its factor: it is
            # reported, not dropped as a dividend would be (HAT-EXEC-144)
            if "split" in lowered or "bonus" in lowered or "consolidat" in lowered:
                out.unparsed.append(purpose)
            else:
                out.ignored += 1
            continue
        instrument = instrument_for(row.get("SYMBOL", "").upper(), ex) if ex else None
        if ex is None or instrument is None:
            out.unparsed.append(purpose)
            continue
        out.records.append(
            ActionRecord(
                instrument,
                ex,
                parsed.action_type,
                factor_to_decimal(parsed.factor),
                purpose,
                SOURCE_ARCHIVE,
            )
        )
    return out


@dataclass
class LoadReport:
    created: int = 0
    unchanged: int = 0
    conflicts: list[tuple[object, date, str]] = field(default_factory=list)


def load_actions(session: Session, records: Sequence[ActionRecord]) -> LoadReport:
    """Store the records idempotently: one row per (instrument, ex_date, action_type), the first
    source to supply a key wins, and a later different factor is reported, not written."""
    report = LoadReport()
    existing = {
        (row.instrument_id, row.ex_date, row.action_type): row
        for row in session.scalars(
            select(HatCorporateAction).where(
                HatCorporateAction.instrument_id.in_({r.instrument_id for r in records})
            )
        )
    }
    for record in records:
        if record.factor <= 0:
            report.conflicts.append(record.key)  # a non-positive factor is never stored
            continue
        row = existing.get(record.key)
        if row is None:
            row = HatCorporateAction(
                instrument_id=record.instrument_id,
                ex_date=record.ex_date,
                action_type=record.action_type,
                adjustment_factor=record.factor,
                raw_text=record.raw_text,
                source=record.source,
            )
            session.add(row)
            existing[record.key] = row
            report.created += 1
        elif Decimal(row.adjustment_factor) == record.factor:
            report.unchanged += 1
        else:
            report.conflicts.append(record.key)
    session.flush()
    return report


@dataclass(frozen=True)
class ActionLookup:
    """What `lookup_actions` found: the records, the symbols whose lookup failed, came back
    incomplete or held a row that could not be read (an unknown action is not "none"), and the
    texts of those unread rows."""

    records: list[ActionRecord] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def lookup_actions(
    client: McpClient, instruments: Mapping[str, object], from_date: date, to_date: date
) -> ActionLookup:
    """`get_corporate_actions` for each symbol (to its instrument id) over the dates, on the
    client's pool. An incomplete page gives no record; a page with an unread row (one `from_mcp`
    cannot parse, or one that is not an object at all) keeps the rows it could read and fails its
    symbol."""

    def one(symbol: str) -> Callable[[], tuple[str, Normalised | None]]:
        def run() -> tuple[str, Normalised | None]:
            try:
                page = client.corporate_actions(symbol, from_date, to_date)
            except McpError:
                return symbol, None
            if page.incomplete:
                return symbol, None
            found = from_mcp(instruments[symbol], page.actions)
            # a row the client could not read as an object is an unread action, not "none"
            found.unparsed.extend(["<unreadable row>"] * page.unreadable)
            return symbol, found

        return run

    results = client.map_calls([one(s) for s in sorted(instruments)])
    return ActionLookup(
        [r for _, n in results if n is not None for r in n.records],
        sorted(s for s, n in results if n is None or n.unparsed),
        [text for _, n in results if n is not None for text in n.unparsed],
    )


def store_actions(session_factory: Callable[[], Session], records: Sequence[ActionRecord]) -> int:
    """Store `records` in one transaction (`load_actions`); the number created. A conflict (a
    factor other than the stored one, or a non-positive one) leaves the stored row as it is and
    is logged with its key, never passed over in silence."""
    if not records:
        return 0
    with session_factory() as session:
        report = load_actions(session, records)
        session.commit()
    for instrument_id, ex_date, action_type in report.conflicts:
        logger.warning(
            "corporate action conflict kept the stored factor: %s %s %s",
            instrument_id,
            ex_date.isoformat(),
            action_type,
        )
    return report.created


def stored_factors(session: Session) -> list[ActionFactor]:
    """Every stored action that changes prices, as the view needs it (a price-neutral row is
    left out: its factor is 1)."""
    return [
        ActionFactor(row.instrument_id, row.ex_date, Decimal(row.adjustment_factor))
        for row in session.scalars(
            select(HatCorporateAction).where(
                HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES)
            )
        )
    ]
