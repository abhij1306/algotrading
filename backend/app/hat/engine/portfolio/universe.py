"""Universe membership as pure rules (HAT-PORT-021..023, HAT-SCH-042).

A universe is a set of instruments over time: each membership is a half-open interval
[effective_from, effective_to) of one instrument, and the members on a date are exactly the
instruments with an interval containing it, delisted ones included for the dates they were
members (point in time, so a backtest is survivorship-free). History is append-only: an edit
never reaches back before what is recorded.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.hat.errors import HatError


class UniverseError(HatError):
    """A membership edit was refused; `code` names the rule and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Interval:
    """One `hat_universe_member` row's dates for an instrument."""

    instrument_id: UUID
    effective_from: date
    effective_to: date | None = None


def active_on(interval: Interval, day: date) -> bool:
    return interval.effective_from <= day and (
        interval.effective_to is None or interval.effective_to > day
    )


def members_on(rows: Iterable[Interval], day: date) -> frozenset[UUID]:
    """The instruments that are members on `day`: those with an interval containing it."""
    return frozenset(r.instrument_id for r in rows if active_on(r, day))


def intervals_valid(rows: Iterable[Interval]) -> bool:
    """Every interval has effective_from before effective_to (when set), and the intervals of
    one instrument do not overlap (HAT-SCH-042)."""
    by_instrument: dict[UUID, list[Interval]] = {}
    for row in rows:
        if row.effective_to is not None and row.effective_from >= row.effective_to:
            return False
        by_instrument.setdefault(row.instrument_id, []).append(row)
    for items in by_instrument.values():
        ordered = sorted(items, key=lambda r: r.effective_from)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective_to is None or earlier.effective_to > later.effective_from:
                return False
    return True


def add_member(rows: Iterable[Interval], instrument_id: UUID, day: date) -> list[Interval]:
    """`rows` with `instrument_id` a member from `day`. Already a member on `day`: no change.
    A `day` before the end of the instrument's recorded history is refused (no rewriting the
    past, so no overlap)."""
    current = list(rows)
    mine = [r for r in current if r.instrument_id == instrument_id]
    if any(active_on(r, day) for r in mine):
        return current
    if any(r.effective_to is None or r.effective_to > day for r in mine):
        raise UniverseError("universe_edit_in_past")
    return [*current, Interval(instrument_id, day, None)]


def remove_member(rows: Iterable[Interval], instrument_id: UUID, day: date) -> list[Interval]:
    """`rows` with `instrument_id` no longer a member from `day`. Not a member on `day`: no
    change. A `day` on or before its interval's start would leave an empty interval and is
    refused; so is a `day` before an end already recorded (no rewriting the past)."""
    out: list[Interval] = []
    for row in rows:
        if row.instrument_id == instrument_id and active_on(row, day):
            if day <= row.effective_from:
                raise UniverseError("universe_empty_interval")
            if row.effective_to is not None:
                raise UniverseError("universe_edit_in_past")
            row = Interval(row.instrument_id, row.effective_from, day)
        out.append(row)
    return out


def effective_members(
    constituents: Collection[UUID], include: Collection[UUID], exclude: Collection[UUID]
) -> frozenset[UUID]:
    """The MVP universe: the index constituents, plus the owner's includes, minus the owner's
    excludes: (C union I) minus X (HAT-PORT-022)."""
    return (frozenset(constituents) | frozenset(include)) - frozenset(exclude)


def plan_sync(
    rows: Iterable[Interval], target: Collection[UUID], day: date
) -> tuple[frozenset[UUID], frozenset[UUID]]:
    """(to add, to remove) so the members on `day` equal `target`."""
    now = members_on(rows, day)
    wanted = frozenset(target)
    return wanted - now, now - wanted


@dataclass(frozen=True)
class MembershipChange:
    """What a change of membership does. It never sells: `forced_exits` is always empty."""

    blocked_entries: frozenset[UUID]  # left the universe: no new entry
    running_to_exit: frozenset[UUID]  # open positions in them: they keep their own exits
    forced_exits: frozenset[UUID] = frozenset()


def membership_change(
    before: Collection[UUID], after: Collection[UUID], open_positions: Collection[UUID]
) -> MembershipChange:
    """Leaving the universe stops entries and never forces a sale (HAT-PORT-023)."""
    left = frozenset(before) - frozenset(after)
    return MembershipChange(left, left & frozenset(open_positions))


def entries_allowed(candidates: Iterable[UUID], members: Collection[UUID]) -> list[UUID]:
    """The candidates that may be entered: members only, in their order."""
    allowed = frozenset(members)
    return [c for c in candidates if c in allowed]
