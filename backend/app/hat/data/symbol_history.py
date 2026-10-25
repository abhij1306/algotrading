"""The symbol history in the database (HAT-DATA-021, 022): which instrument held a symbol on a
day. Intervals of one symbol never overlap across instruments (`claim`); a rename, a delisting
or a relisting moves them (`apply_symbol_change`, `close_history`, `reopen_history`), the
recorded renames seed them (`seed_symbol_history`) and the one-off repair closes old overlaps
(`repair_symbol_history`). `holders` and `resolve_symbol` are the one lookup of "which instrument
was SYMBOL on DAY". Every function writes in the caller's session and is idempotent.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..models import HatInstrument, HatInstrumentSymbolHistory
from .corporate_actions import lock_reference_data

EPOCH = date(1900, 1, 1)


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


def held_by_another(session: Session, symbol: str, on: date) -> bool:
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


def holders(session: Session, market_id: uuid.UUID, symbol: str, on: date) -> list[HatInstrument]:
    """The instruments whose symbol history covers (symbol, on), the preferred first (active,
    then the lowest id). The writers keep one holder per symbol and day (`claim`; on Postgres an
    EXCLUDE constraint, once `repair_symbol_history` has run), so there is at most one."""
    found = session.scalars(
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
    return sorted(found, key=lambda i: _preference(i.active, i.id))


def resolve_symbol(
    session: Session, market_id: uuid.UUID, symbol: str, on: date
) -> HatInstrument | None:
    """The one instrument whose symbol history covers (symbol, on) (`holders`)."""
    found = holders(session, market_id, symbol, on)
    return found[0] if found else None


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


def close_history(session: Session, instrument_id: uuid.UUID, on: date) -> None:
    """A delisted instrument's open interval ends on `on`."""
    rows = _history(session, instrument_id)
    if rows and rows[-1].valid_to is None and rows[-1].valid_from < on:
        rows[-1].valid_to = on


def reopen_history(session: Session, instrument_id: uuid.UUID, symbol: str, today: date) -> None:
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


def open_interval(session: Session, instrument_id: uuid.UUID, symbol: str, start: date) -> None:
    """A new instrument holds `symbol` from `start`, open-ended; the interval another instrument
    holds it in ends there (`claim`)."""
    row = _new_interval(instrument_id, symbol, start, None)
    session.add(row)
    session.flush()
    _settle(session, row)
