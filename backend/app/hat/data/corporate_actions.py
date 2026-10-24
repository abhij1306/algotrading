"""Corporate actions: parsing, storage in `hat_corporate_action` and the factors the adjusted view
uses (HAT-DATA-052, 053, 054, 055, 094; HAT-DEC-B27).

A factor multiplies prices before the ex-date (a 1:2 split is 0.5). Sources, in the order they
win on a conflict: the MCP's structured `adjustmentFactor`, then NSE's archive CSV text parsed
with HAT's own text helpers (`app.hat.data.ratios`). A stored action is never
changed by a later load: a different factor for a stored key is reported as a conflict.

An MCP action that changes no price (factor 1: a dividend, an AGM) is stored too, as
`dividend` or `notice` (PR 47, HAT-DATA-052), and so is a bonus of preference shares or
debentures (`non_equity_bonus`, D1), whatever factor the MCP gives it: Fyers cancels a resting
GTT before any ex-date, so the morning GTT check needs every ex-date of a held name
(HAT-EXEC-145). Such a row is never
a price factor (`stored_factors`) and never an action the rescale handles or flags.

`lookup_actions` is the one MCP lookup, pooled on the client: the nightly ingest, the next-session
and quarantine lookups and the history build all read actions through it, so an MCP page means
the same everywhere (a failed, incomplete or unreadable page fails its symbol).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.hat.data.ratios import find_bonus_ratio, split_pair_numbers

from .. import events
from ..errors import HatError
from ..models import HatCorporateAction, HatEvent, HatInstrument
from ..money import exact
from ..notify import Notifier
from .mcp_client import McpClient, McpError
from .store import FACTOR_MAX, FACTOR_MIN, ActionFactor

logger = logging.getLogger(__name__)

SOURCE_MCP = "mcp"
SOURCE_ARCHIVE = "archive"
TYPE_SPLIT = "split"
TYPE_BONUS = "bonus"
TYPE_OTHER = "other"
TYPE_DIVIDEND = "dividend"
TYPE_NOTICE = "notice"  # any other action the MCP gives factor 1: no price changes
PRICE_NEUTRAL_TYPES = (TYPE_DIVIDEND, TYPE_NOTICE)
ACTION_TYPES = (TYPE_SPLIT, TYPE_BONUS, TYPE_OTHER, *PRICE_NEUTRAL_TYPES)

_REFERENCE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hat_reference_data'))"


def lock_reference_data(session: Session) -> None:
    """Hold the reference-data lock until the transaction ends (audit M4). The writers of the
    actions, the lineage and the symbol history (`build_lineage`, `override_lineage`,
    `correct_action`, `add_action`, `ca_import.apply`, `repair_symbol_history`) take it, so an
    owner's correction never lands between a nightly build's read and its write. A no-op on
    SQLite, which runs one writer at a time."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(_REFERENCE_LOCK_SQL))


EVENT_CORRECTED = "corporate_action.corrected"  # the owner's correction (`correct_action`)
STORED_PLACES = 12  # a factor such as 2/3 is stored rounded to 12 places

_MCP_TYPES = {"SPLIT": TYPE_SPLIT, "BONUS": TYPE_BONUS}

# A bonus of preference shares or debentures ("Bonus NCRPS 4:1", "Bonus Debentures 6:1", "Bonus
# Deb1:1", "Bonus Preference Shares 21:1", often under a scheme of arrangement) issues no equity:
# the share count stays and the price drops by about the instrument's value, as on a dividend.
# Read as an equity bonus it fakes a 5x-47x jump in the adjusted history (TVSMOTOR 2025).
_NON_EQUITY_BONUS = re.compile(r"bonus\W*(?:n?c?rps|ncds?\b|deb(?:entures?)?(?![a-z])|pref)")


def non_equity_bonus(text: str) -> bool:
    """True when `text` names a bonus of preference shares or debentures, not of equity."""
    return _NON_EQUITY_BONUS.search(text.lower()) is not None


@dataclass(frozen=True)
class ParsedAction:
    action_type: str
    factor: Fraction


def parse_action_text(text: str) -> ParsedAction | None:
    """A face-value split from a to b gives b / a; a bonus of x new for y held gives
    y / (x + y). Anything else, or a factor outside (0, 1), is None: never a guessed factor. A
    bonus of preference shares or debentures (`non_equity_bonus`) is None too: no price factor."""
    lowered = text.lower()
    if "split" in lowered or "sub-division" in lowered or "sub division" in lowered:
        pair = split_pair_numbers(lowered)
        if pair is not None:
            old, new = pair
            factor = new / old
            if 0 < factor < 1:
                return ParsedAction(TYPE_SPLIT, factor)
        return None
    ratio = None if non_equity_bonus(lowered) else find_bonus_ratio(lowered)
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
    cash_amount: Decimal | None = None  # a dividend's rupees per share (`ca_dividends`)

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
    other types with a factor other than 1 are stored as `other`; an action of factor 1, or a
    bonus of preference shares or debentures whatever its factor, is stored price-neutral, as
    `dividend` or `notice` with factor 1 (its ex-date is what HAT needs)."""
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
        if kind != TYPE_SPLIT and non_equity_bonus(text):
            factor = Decimal(1)
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
            if non_equity_bonus(lowered):  # no price factor, and the archive keeps no dividend
                out.ignored += 1
                continue
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
    held: int = 0  # keys the owner deleted or retyped away (`correct_action`): never re-created


def load_actions(session: Session, records: Sequence[ActionRecord]) -> LoadReport:
    """Store the records idempotently: one row per (instrument, ex_date, action_type), the first
    source to supply a key wins, and a later different factor (or dividend cash amount) is
    reported, not written. A key
    the owner vacated (`correct_action`: deleted, or retyped away) is never re-created: the
    owner's correction wins over every later load."""
    report = LoadReport()
    held = owner_vacated(session, {r.instrument_id for r in records})
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
        if row is None and record.key in held:
            report.held += 1
            continue
        if row is None:
            row = HatCorporateAction(
                instrument_id=record.instrument_id,
                ex_date=record.ex_date,
                action_type=record.action_type,
                adjustment_factor=record.factor,
                raw_text=record.raw_text,
                source=record.source,
                cash_amount=record.cash_amount,
            )
            session.add(row)
            existing[record.key] = row
            report.created += 1
        elif Decimal(row.adjustment_factor) == record.factor and (
            record.cash_amount is None
            or row.cash_amount is None
            or Decimal(row.cash_amount) == record.cash_amount
        ):
            report.unchanged += 1  # a stored amount is never rewritten, nor a missing one added
        else:
            report.conflicts.append(record.key)
    session.flush()
    return report


def owner_vacated(session: Session, instrument_ids: Iterable[object]) -> set[tuple]:
    """(instrument, ex-date, type) of every stored key the owner corrected, read from the journal
    (`corporate_action.corrected`); a key still stored is matched first, so only a vacated one
    counts."""
    ids = set(instrument_ids)
    if not ids:
        return set()
    events = session.scalars(
        select(HatEvent).where(
            HatEvent.event_type == EVENT_CORRECTED,
            HatEvent.aggregate_type == "instrument",
            HatEvent.aggregate_id.in_(ids),
        )
    )
    return {
        (e.aggregate_id, date.fromisoformat(e.payload["ex_date"]), e.payload["before"]["type"])
        for e in events
    }


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


# A nightly lookup found a factor other than the stored one (D220): the stored factor stays, and
# the owner is told, since one of the two is wrong.
ALERT_ACTION_CONFLICT = "corporate_action.conflict"


def store_actions(
    session_factory: Callable[[], Session],
    records: Sequence[ActionRecord],
    conflicts_to: tuple[Notifier, date] | None = None,
) -> LoadReport:
    """Store `records` in one transaction (`load_actions`). A conflict (a factor other than the
    stored one, or a non-positive one) leaves the stored row as it is, is logged with its key
    and, given `conflicts_to` (the notifier and the trade date), alerted once per key with its
    ex-date and type; never passed over in silence."""
    if not records:
        return LoadReport()
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
        if conflicts_to is not None:
            notifier, trade_date = conflicts_to
            notifier.alert(
                ALERT_ACTION_CONFLICT,
                trade_date,
                {
                    "instrument": str(instrument_id),
                    "ex_date": ex_date.isoformat(),
                    "type": action_type,
                },
            )
    return report


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


# --- the owner's correction ---------------------------------------------------------------------

NOTE_MAX = 500


class ActionCorrectionError(HatError):
    """A correction was refused before anything was written; `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Correction:
    """What the owner asks of one stored action: `factor` (and `new_type`, default the stored
    type) replaces it, or `factor` None deletes it."""

    isin: str
    ex_date: date
    action_type: str
    factor: Decimal | None
    new_type: str | None = None
    note: str = ""


def check_correction(correction: Correction) -> tuple[str, Decimal] | None:
    """The (type, factor) a replacement stores, None for a delete. A price-neutral type takes
    factor 1 and a price-changing type any other factor the view can hold; else
    `ActionCorrectionError` (`bad_action_type`, `bad_factor`, `note_required`)."""
    if not correction.note.strip() or len(correction.note) > NOTE_MAX:
        raise ActionCorrectionError("note_required")
    target = correction.new_type or correction.action_type
    if correction.action_type not in ACTION_TYPES or target not in ACTION_TYPES:
        raise ActionCorrectionError("bad_action_type")
    if correction.factor is None:
        return None
    factor = correction.factor
    neutral = target in PRICE_NEUTRAL_TYPES
    # the view holds a cumulative factor only within [FACTOR_MIN, FACTOR_MAX) (store.py)
    if not factor.is_finite() or not FACTOR_MIN <= factor < FACTOR_MAX or (factor == 1) != neutral:
        raise ActionCorrectionError("bad_factor")
    return target, factor


def correct_action(
    session: Session, correction: Correction, *, chat_id: int, user_id: uuid.UUID, now: datetime
) -> dict[str, object]:
    """Replace or delete one stored action, keyed by (ISIN, ex-date, type), and journal it as
    `corporate_action.corrected` (actor user, via cli) in the same transaction: the fix for a
    `corporate_action.conflict` or a misparsed factor (D1), which no later load overwrites (a
    stored key is never rewritten by a load, and a key it vacates is never re-created:
    `owner_vacated`). A replacement whose key another stored row already
    holds with the same factor deletes the corrected row (the right fact is stored); with
    another factor it is refused (`action_key_taken`). Refused too: an unknown ISIN
    (`instrument_not_found`), no such action (`action_not_found`) or `check_correction`'s codes.
    The adjusted view reads the change at the next features run (its digest covers the
    factors)."""
    replacement = check_correction(correction)
    lock_reference_data(session)
    instrument = session.scalar(select(HatInstrument).where(HatInstrument.isin == correction.isin))
    if instrument is None:
        raise ActionCorrectionError("instrument_not_found")
    row = session.scalar(
        select(HatCorporateAction)
        .where(
            HatCorporateAction.instrument_id == instrument.id,
            HatCorporateAction.ex_date == correction.ex_date,
            HatCorporateAction.action_type == correction.action_type,
        )
        .with_for_update()
    )
    if row is None:
        raise ActionCorrectionError("action_not_found")
    before = {"type": row.action_type, "factor": str(row.adjustment_factor)}
    after: dict[str, object] = {"deleted": True}
    if replacement is None:
        session.delete(row)
    else:
        new_type, factor = replacement
        holder = (
            session.scalar(
                select(HatCorporateAction).where(
                    HatCorporateAction.instrument_id == instrument.id,
                    HatCorporateAction.ex_date == correction.ex_date,
                    HatCorporateAction.action_type == new_type,
                )
            )
            if new_type != row.action_type
            else None
        )
        if holder is not None and Decimal(holder.adjustment_factor) != factor:
            raise ActionCorrectionError("action_key_taken")
        if holder is not None:
            session.delete(row)
            after = {"type": new_type, "factor": str(factor), "kept_stored": True}
        else:
            row.action_type, row.adjustment_factor = new_type, factor
            after = {"type": new_type, "factor": str(factor)}
    payload = {
        "chat_id": chat_id,
        "via": "cli",
        "isin": correction.isin,
        "ex_date": correction.ex_date.isoformat(),
        "before": before,
        "after": after,
        "note": correction.note.strip(),
    }
    events.record(
        session,
        aggregate_type="instrument",
        aggregate_id=instrument.id,
        event_type=EVENT_CORRECTED,
        actor="user",
        payload=payload,
        user_id=user_id,
        occurred_at=now,
    )
    session.flush()
    return {"symbol": instrument.symbol, **{k: v for k, v in payload.items() if k != "chat_id"}}
