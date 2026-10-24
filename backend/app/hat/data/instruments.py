"""Instrument master, symbol history, broker symbols, price bands and industries in the
database (HAT-DATA-020..026). Every function writes in the caller's session and is idempotent:
running it twice with the same input changes nothing the second time.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..errors import HatError
from ..ids import uuid7
from ..models import (
    HatBrokerSymbol,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatPriceBand,
)
from ..models.strategy import BROKER_FYERS
from ..time import utc_now
from .corporate_actions import lock_reference_data
from .reference import BandRow, IndustryRow, Security, one_per_isin
from .symbols import SymbolMaster

logger = logging.getLogger(__name__)

EPOCH = date(1900, 1, 1)
CODE_LISTING_SHRUNK = "reference_listing_shrunk"
# A listing that drops more than half of at least this many active instruments is treated as a
# broken source, not as mass delisting.
SHRINK_GUARD_MIN_ACTIVE = 100


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


# The series a Fyers symbol is written for; BZ, BL, IL and the rest get none.
FYERS_SERIES = frozenset({"EQ", "BE"})
CODE_ISIN_NOT_MERGED = "isin_not_merged"


def by_isin(instruments: Iterable[HatInstrument]) -> dict[str, HatInstrument]:
    """The instruments by ISIN. Two rows for one ISIN mean the database predates one id per ISIN
    (audit D5): `python -m app.hat.data.isin_merge up` has not run, so nothing may write."""
    out: dict[str, HatInstrument] = {}
    for instrument in instruments:
        if out.setdefault(instrument.isin, instrument) is not instrument:
            raise ReferenceDataError(CODE_ISIN_NOT_MERGED)
    return out


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


class Interval(NamedTuple):
    """One `hat_instrument_symbol_history` row as plain values; `valid_to` None is open."""

    instrument_id: uuid.UUID
    symbol: str
    valid_from: date
    valid_to: date | None


def claim(
    start: date, end: date | None, others: Sequence[tuple[date, date | None]]
) -> tuple[list[date | None], date | None]:
    """One instrument takes a symbol from `start` until `end` (None: open); `others` are the
    (valid_from, valid_to) intervals other instruments hold it in. Returns their new ends and
    the new interval's: one that began earlier and is still open at `start` closes there (the
    new holder's first day), and the new interval ends where a later one begins. One that begins
    on `start` itself is left alone: the pair is a conflict the database refuses (audit M3)."""
    ends = [start if f < start and (t is None or t > start) else t for f, t in others]
    later = [f for f, _ in others if f > start]
    if later and (end is None or min(later) < end):
        end = min(later)
    return ends, end


def _settle(session: Session, row: HatInstrumentSymbolHistory) -> None:
    """Apply `claim` to `row`, just written, against every other instrument's interval of its
    symbol, so no two instruments hold one symbol on one day. Its callers hold the
    reference-data lock, so another writer's interval is committed, and seen here, or waits."""
    others = list(
        session.scalars(
            select(HatInstrumentSymbolHistory).where(
                HatInstrumentSymbolHistory.symbol == row.symbol,
                HatInstrumentSymbolHistory.instrument_id != row.instrument_id,
            )
        )
    )
    ends, end = claim(row.valid_from, row.valid_to, [(o.valid_from, o.valid_to) for o in others])
    if end != row.valid_to:
        row.valid_to = end
    for other, other_end in zip(others, ends, strict=True):
        if other_end != other.valid_to:
            other.valid_to = other_end


def _held_by_another(session: Session, symbol: str, on: date) -> bool:
    return (
        session.scalar(
            select(HatInstrumentSymbolHistory.instrument_id)
            .where(
                HatInstrumentSymbolHistory.symbol == symbol,
                HatInstrumentSymbolHistory.valid_from <= on,
                (HatInstrumentSymbolHistory.valid_to.is_(None))
                | (HatInstrumentSymbolHistory.valid_to > on),
            )
            .limit(1)
        )
        is not None
    )


def _place_symbol(
    session: Session,
    rows: list[HatInstrumentSymbolHistory],
    instrument_id: uuid.UUID,
    new_symbol: str,
    effective: date,
    old_symbol: str | None,
) -> bool:
    """Make `new_symbol` hold from `effective` among the instrument's `rows`."""
    exact = next((r for r in rows if r.valid_from == effective), None)
    if exact is not None:
        changed = exact.symbol != new_symbol
        exact.symbol = new_symbol
        return changed
    covering = next(
        (
            r
            for r in reversed(rows)
            if r.valid_from < effective and (r.valid_to is None or r.valid_to > effective)
        ),
        None,
    )
    if covering is None:
        # No row covers `effective` (before the first row, or in a gap after a delisting): the
        # new symbol holds until the next row begins.
        following = next((r for r in rows if r.valid_from > effective), None)
        end = following.valid_from if following is not None else None
        session.add(_new_interval(instrument_id, new_symbol, effective, end))
        return True
    if old_symbol is not None and covering.symbol == new_symbol:
        covering.symbol = old_symbol  # the legacy record says what it was before
    if covering.symbol == new_symbol:
        return False
    end = covering.valid_to
    following = next((r for r in rows if end is not None and r.valid_from == end), None)
    if following is not None and following.symbol == new_symbol:
        # the new symbol already holds from `end` (the master saw the rename only on that day):
        # it becomes one interval from `effective`
        end = following.valid_to
        session.delete(following)
    session.add(_new_interval(instrument_id, new_symbol, effective, end))
    covering.valid_to = effective
    return True


def apply_symbol_change(
    session: Session,
    instrument_id: uuid.UUID,
    new_symbol: str,
    effective: date,
    *,
    old_symbol: str | None = None,
) -> bool:
    """Make `new_symbol` the instrument's symbol from `effective`, keeping the validity
    intervals of its rows disjoint, and those of other instruments holding the symbol too
    (`claim`). `old_symbol`, when known (the legacy history), labels the interval before
    `effective`. Returns True when any row changed.
    """
    lock_reference_data(session)
    changed = _place_symbol(
        session, _history(session, instrument_id), instrument_id, new_symbol, effective, old_symbol
    )
    session.flush()
    taken = [
        r
        for r in _history(session, instrument_id)
        if r.valid_from <= effective and (r.valid_to is None or r.valid_to > effective)
    ]  # the row that holds `new_symbol` from `effective`
    if old_symbol is not None and not any(
        r.valid_from < effective for r in _history(session, instrument_id)
    ):
        taken.append(_new_interval(instrument_id, old_symbol, EPOCH, effective))
        session.add(taken[-1])
        changed = True
    session.flush()
    for row in taken:
        _settle(session, row)
    session.flush()
    return changed


def resolve_symbol(
    session: Session, market_id: uuid.UUID, symbol: str, on: date
) -> HatInstrument | None:
    """The one instrument whose symbol history covers (symbol, on). The writers keep one holder
    per symbol and day (`claim`; on Postgres an EXCLUDE constraint, once
    `repair_symbol_history` has run); were there several, the active one would win, then the
    lowest id."""
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
    return min(candidates, key=lambda i: _preference(i.active, i.id))


def _preference(active: bool, instrument_id: uuid.UUID) -> tuple[bool, bytes]:
    """The order several instruments sharing a symbol are chosen in: active first, then the
    lowest id."""
    return (not active, instrument_id.bytes)


def symbol_ids(rows: Iterable[tuple[uuid.UUID, str, bool]]) -> dict[str, uuid.UUID]:
    """Each symbol's instrument from `(id, symbol, active)` rows, by `resolve_symbol`'s rule
    (active first, then the lowest id), whatever order the rows come in: a symbol an old,
    inactive ISIN still carries maps to the live instrument."""
    best: dict[str, tuple[bool, uuid.UUID]] = {}
    for instrument_id, symbol, active in rows:
        held = best.get(symbol)
        if held is None or _preference(active, instrument_id) < _preference(*held):
            best[symbol] = (active, instrument_id)
    return {symbol: instrument_id for symbol, (_, instrument_id) in best.items()}


@dataclass(frozen=True)
class SymbolRename:
    """One recorded rename: `old_symbol` (None when unknown) became `new_symbol` on
    `effective_from`; `isin` (when known) names the instrument."""

    old_symbol: str | None
    new_symbol: str
    effective_from: date
    isin: str | None = None


def seed_symbol_history(
    session: Session, market_id: uuid.UUID, renames: Iterable[SymbolRename]
) -> int:
    """Seed `hat_instrument_symbol_history` from `renames`, a source the caller injects.

    Each rename whose new symbol is a current instrument (matched by ISIN when the record has
    one, else by the new symbol) becomes a change at its effective date. Changes are applied
    latest first: each one labels the interval before it, which the next (earlier) change then
    splits. HAT reads no legacy table: the caller supplies the records (an export script, or the
    NSE data of PR 7).
    Idempotent. Returns how many renames matched no instrument.
    """
    legacy = sorted(
        (r for r in renames if r.new_symbol), key=lambda r: r.effective_from, reverse=True
    )
    unmatched = 0
    for record in legacy:
        if record.old_symbol == record.new_symbol:
            continue
        instrument = None
        if record.isin:
            instrument = session.scalar(
                select(HatInstrument).where(
                    HatInstrument.market_id == market_id, HatInstrument.isin == record.isin
                )
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


# --- the symbol history repair (audit M3) ------------------------------------------------------

EVENT_REPAIRED = "symbol_history.repaired"
SKIP_NO_BARS = "no_bars"  # a holder has no bar under the symbol: nothing says who held it when
SKIP_SAME_FIRST_BAR = "same_first_bar"  # two holders' first bars fall on one day
SKIP_KEY_TAKEN = "key_taken"  # a moved start would collide with another row of the instrument


def _overlap(a: Interval, b: Interval) -> bool:
    return a.valid_from < (b.valid_to or date.max) and b.valid_from < (a.valid_to or date.max)


def overlapping_pairs(intervals: Iterable[Interval]) -> int:
    """How many pairs of intervals of one symbol, held by two instruments, overlap."""
    by_symbol: dict[str, list[Interval]] = defaultdict(list)
    for row in intervals:
        by_symbol[row.symbol].append(row)
    return sum(
        a.instrument_id != b.instrument_id and _overlap(a, b)
        for rows in by_symbol.values()
        for i, a in enumerate(rows)
        for b in rows[i + 1 :]
    )


@dataclass
class RepairPlan:
    """The rows to change (before, after; after None: deleted) and the symbols left alone."""

    changes: list[tuple[Interval, Interval | None]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def _clipped(row: Interval, lo: date | None, hi: date | None) -> Interval | None:
    start = row.valid_from if lo is None else max(row.valid_from, lo)
    end = row.valid_to if hi is None else min(row.valid_to or hi, hi)
    if end is not None and end <= start:
        return None
    return row._replace(valid_from=start, valid_to=end)


def repair_plan(
    intervals: Sequence[Interval], first_bars: Mapping[tuple[uuid.UUID, str], date]
) -> RepairPlan:
    """Close the overlaps of the symbol history from the bars: for each symbol two instruments
    hold at once, its holders in the order of their first bar under it, each holder keeps it
    from its own first bar (the first holder: from its interval's start) until the next
    holder's first bar. Rows of a symbol without an overlap are never touched; a symbol whose
    order the bars cannot give (`SKIP_*`) is left as it is and reported."""
    plan = RepairPlan()
    by_symbol: dict[str, list[Interval]] = defaultdict(list)
    starts: set[tuple[uuid.UUID, date]] = set()
    for row in intervals:
        by_symbol[row.symbol].append(row)
        starts.add((row.instrument_id, row.valid_from))
    for symbol, rows in sorted(by_symbol.items()):
        if not overlapping_pairs(rows):
            continue
        holders = {r.instrument_id: first_bars.get((r.instrument_id, symbol)) for r in rows}
        if any(day is None for day in holders.values()):
            plan.skipped[symbol] = SKIP_NO_BARS
            continue
        firsts: dict[uuid.UUID, date] = {k: v for k, v in holders.items() if v is not None}
        if len(set(firsts.values())) < len(firsts):
            plan.skipped[symbol] = SKIP_SAME_FIRST_BAR
            continue
        order = sorted(firsts, key=firsts.__getitem__)
        window = {
            holder: (
                firsts[holder] if i else None,
                firsts[order[i + 1]] if i + 1 < len(order) else None,
            )
            for i, holder in enumerate(order)
        }
        changes = [(r, _clipped(r, *window[r.instrument_id])) for r in rows]
        changes = [(r, after) for r, after in changes if after != r]
        if any(
            after is not None
            and after.valid_from != r.valid_from
            and (r.instrument_id, after.valid_from) in starts
            for r, after in changes
        ):
            plan.skipped[symbol] = SKIP_KEY_TAKEN
            continue
        plan.changes.extend(changes)
    return plan


def _interval_text(row: Interval | None) -> str | None:
    if row is None:
        return None
    end = row.valid_to.isoformat() if row.valid_to is not None else "open"
    return f"{row.valid_from.isoformat()}..{end}"


def repair_symbol_history(
    session: Session,
    first_bars: Mapping[tuple[uuid.UUID, str], date],
    *,
    apply: bool,
    owner: tuple[int, uuid.UUID, datetime],
) -> dict[str, object]:
    """The one-off repair of `repair_plan` (audit M3), under the reference-data lock. With
    `apply` each changed row is journalled `symbol_history.repaired` (actor user, via cli) in
    the caller's transaction; without it nothing is written. `owner` is (chat id, user id, now).
    `first_bars` is each (instrument, symbol)'s first raw bar (`renames.symbol_first_bars`)."""
    lock_reference_data(session)
    rows = {
        Interval(r.instrument_id, r.symbol, r.valid_from, r.valid_to): r
        for r in session.scalars(select(HatInstrumentSymbolHistory))
    }
    isins = dict(session.execute(select(HatInstrument.id, HatInstrument.isin)).tuples().all())
    before = overlapping_pairs(rows)
    plan = repair_plan(list(rows), first_bars)
    after = list(rows)
    for old, new in plan.changes:
        after.remove(old)
        if new is not None:
            after.append(new)
    lines = [
        f"{old.symbol} {isins[old.instrument_id]} {_interval_text(old)} -> {_interval_text(new)}"
        for old, new in plan.changes
    ]
    if apply:
        chat_id, user_id, now = owner
        for old, new in plan.changes:
            if new is None:
                session.delete(rows[old])
            else:
                rows[old].valid_from, rows[old].valid_to = new.valid_from, new.valid_to
            events.record(
                session,
                aggregate_type="instrument",
                aggregate_id=old.instrument_id,
                event_type=EVENT_REPAIRED,
                actor="user",
                payload={
                    "chat_id": chat_id,
                    "via": "cli",
                    "symbol": old.symbol,
                    "isin": isins[old.instrument_id],
                    "before": _interval_text(old),
                    "after": _interval_text(new),
                },
                user_id=user_id,
                occurred_at=now,
            )
        session.flush()
    return {
        "applied": apply,
        "overlaps_before": before,
        "overlaps_after": overlapping_pairs(after),
        "rows_changed": sum(new is not None for _, new in plan.changes),
        "rows_deleted": sum(new is None for _, new in plan.changes),
        "skipped": plan.skipped,
        "changes": lines,
    }


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
        row = rows[-1]
        row.valid_to = None
        row.symbol = symbol
    else:
        row = _new_interval(instrument_id, symbol, today, None)
        session.add(row)
    session.flush()
    _settle(session, row)


def _create_instrument(
    session: Session, market_id: uuid.UUID, security: Security, today: date
) -> None:
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
    # A symbol another instrument holds today (a new ISIN of a listed company: its listing date
    # is the company's) is taken from today; else from the listing date.
    start = security.listed_on or today
    if _held_by_another(session, security.symbol, today):
        start = today
    row = _new_interval(new_id, security.symbol, start, None)
    session.add(row)
    session.flush()
    _settle(session, row)


def _refresh_instrument(
    session: Session, row: HatInstrument, security: Security, today: date, report: UpsertReport
) -> bool:
    """Bring one existing instrument in line with the listing; True when anything changed."""
    touched = False
    if row.series != security.series:
        # The ISIN moved to another series (EQ to BE, say): same instrument, new broker symbol.
        row.previous_series, row.series, row.series_since = row.series, security.series, today
        touched = True
    if row.symbol != security.symbol:
        apply_symbol_change(session, row.id, security.symbol, today)
        row.symbol = security.symbol
        report.renamed += 1
        touched = True
    for attr in ("name", "face_value", "listed_on"):
        value = getattr(security, attr)
        if (attr != "listed_on" or value is not None) and getattr(row, attr) != value:
            setattr(row, attr, value)
            touched = True
    if not row.active:
        row.active = True
        row.delisted_on = None
        _reopen_history(session, row.id, security.symbol, today)
        report.reactivated += 1
        touched = True
    return touched


def upsert_instruments(
    session: Session,
    market_id: uuid.UUID,
    securities: Iterable[Security],
    *,
    today: date,
    complete: bool = True,
    still_listed: Collection[tuple[str, str]] = (),
) -> UpsertReport:
    """Bring `hat_instrument` in line with a listing. Instruments are keyed by (market, isin),
    so a rename or a series change keeps the id (a rename records a history row); an instrument
    missing from a `complete` listing is marked delisted, never deleted. `still_listed` holds
    (symbol, series) pairs the source lists but this listing has no ISIN row for: they are not
    delisted. An ISIN listed under two series keeps the best-ranked one (`one_per_isin`)."""
    lock_reference_data(session)  # the symbol history's writers, one at a time (`claim`)
    listing = {s.isin: s for s in one_per_isin(securities)}
    existing = by_isin(
        session.scalars(select(HatInstrument).where(HatInstrument.market_id == market_id))
    )
    report = UpsertReport()
    active_before = sum(1 for i in existing.values() if i.active)
    seen_active = sum(1 for key in listing if key in existing and existing[key].active)
    if complete and active_before >= SHRINK_GUARD_MIN_ACTIVE and seen_active * 2 < active_before:
        raise ReferenceDataError(CODE_LISTING_SHRUNK)
    for key, security in listing.items():
        row = existing.get(key)
        if row is None:
            _create_instrument(session, market_id, security, today)
            report.created += 1
        elif _refresh_instrument(session, row, security, today, report):
            report.updated += 1
    if complete:
        kept = set(still_listed)
        for key, row in existing.items():
            if key in listing or not row.active or (row.symbol, row.series) in kept:
                continue
            row.active = False
            row.delisted_on = today
            _close_history(session, row.id, today)
            report.delisted += 1
    session.flush()
    return report


def register_history_instrument(
    session: Session,
    market_id: uuid.UUID,
    *,
    isin: str,
    series: str,
    symbol: str,
    first_seen: date,
) -> uuid.UUID:
    """The instrument of `isin`, created inactive under `series` when the master does not have
    it: a security seen only in old bhavcopy files (delisted, merged or renamed away) must still
    own its history (HAT-DATA-021). Name and face value are unknown here (the symbol and 0 stand
    in) until a weekly refresh lists it. Repeating the call, under any series, returns the same
    row."""
    lock_reference_data(session)
    row = session.scalar(
        select(HatInstrument).where(
            HatInstrument.market_id == market_id, HatInstrument.isin == isin
        )
    )
    if row is not None:
        return row.id
    new_id = uuid7()
    session.add(
        HatInstrument(
            id=new_id,
            market_id=market_id,
            isin=isin,
            series=series,
            symbol=symbol,
            name=symbol,
            face_value=Decimal(0),
            listed_on=first_seen,
            delisted_on=None,
            active=False,
        )
    )
    session.flush()
    interval = _new_interval(new_id, symbol, first_seen, None)
    session.add(interval)
    session.flush()
    _settle(session, interval)  # the previous holder's interval ends at this first bar
    session.flush()
    return new_id


# --- broker symbols (HAT-DATA-024) -------------------------------------------------------------


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


def fyers_instruments(session: Session) -> list[tuple[str, str]]:
    """(database symbol, Fyers symbol) of every active instrument that has a Fyers row, sorted
    by symbol: the instrument list HAT's broker adapter reports. It comes from `hat_instrument`
    and `hat_broker_symbol` only, so an index, a delisted stock or a symbol nobody mapped is never
    in it (HAT-DATA-024)."""
    rows = session.execute(
        select(HatInstrument.symbol, HatBrokerSymbol.broker_symbol)
        .join(HatBrokerSymbol, HatBrokerSymbol.instrument_id == HatInstrument.id)
        .where(HatBrokerSymbol.broker == BROKER_FYERS, HatInstrument.active.is_(True))
        .order_by(HatInstrument.symbol, HatBrokerSymbol.broker_symbol)
    ).all()
    return [(symbol, broker_symbol) for symbol, broker_symbol in rows]


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
