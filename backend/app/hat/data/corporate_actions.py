"""Corporate actions: parsing, storage in `hat_corporate_action` and the factors the adjusted view
uses (HAT-DATA-052, 053, 054, 055, 094; HAT-DEC-B27).

A factor multiplies prices before the ex-date (a 1:2 split is 0.5). Sources, in the order they
win on a conflict: the MCP's structured `adjustmentFactor`, then NSE's archive CSV text parsed
with HAT's own text helpers (`app.hat.data.ratios`). A stored action is never
changed by a later load: a different factor for a stored key is reported as a conflict.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data.ratios import find_bonus_ratio, split_pair_numbers

from ..models import HatCorporateAction
from .store import ActionFactor

SOURCE_MCP = "mcp"
SOURCE_ARCHIVE = "archive"
TYPE_SPLIT = "split"
TYPE_BONUS = "bonus"
TYPE_OTHER = "other"
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
    ignored: int = 0  # dividends and other actions that do not change prices


def _iso(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def from_mcp(instrument_id: object, rows: Iterable[dict]) -> Normalised:
    """`get_corporate_actions` rows of one instrument. Splits and bonuses keep the MCP factor;
    other types with a factor other than 1 are stored as `other`; dividends are ignored."""
    out = Normalised()
    for row in rows:
        ex = _iso(row.get("exDate"))
        raw_factor = row.get("adjustmentFactor")
        text = str(row.get("purpose") or "")
        kind = _MCP_TYPES.get(str(row.get("actionType") or "").upper(), TYPE_OTHER)
        try:
            factor = Decimal(str(raw_factor))
        except Exception:
            factor = None
        if ex is None or factor is None or not factor.is_finite() or factor <= 0:
            out.unparsed.append(text)
            continue
        if factor == 1:
            out.ignored += 1
            continue
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
            if "split" in lowered or "bonus" in lowered:
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


def stored_factors(session: Session) -> list[ActionFactor]:
    """Every stored action as the view needs it."""
    return [
        ActionFactor(row.instrument_id, row.ex_date, Decimal(row.adjustment_factor))
        for row in session.scalars(select(HatCorporateAction))
    ]
