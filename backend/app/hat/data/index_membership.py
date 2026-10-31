"""Point-in-time index membership (QA5): which instruments were in an index (`NIFTY 500`; the
table and the loader take any index by name) on each day, from the provider's change notices.

NSE publishes only today's list, so the history is rebuilt outside HAT by undoing each change
notice from today's list back (`undo_changes`) and written to a CSV, one row per membership
interval of one index:

    index_name,isin,symbol_at_the_time,from_date,to_date,source_notice

`from_date` is the first session the instrument was a member (the change's effective date, or
the first covered day for a member from before the history starts) and `to_date` the first
session it no longer was (empty: a member on the list's `as_of` day). `load_membership` (the
admin command `load-index-membership`) maps each ISIN to its instrument and replaces the index's
`hat_index_member` rows with the file's, in one transaction: idempotent, and a dry run unless
applied. `hat_index_membership` records the source, its digest, the covered days and the count
check (`off_size`: every change date whose member count is not the index's size, a documented
temporary vacancy or an error in the history). The backtest resolves its universe from these
rows (`backtest/data.point_in_time_members`) and refuses a window they do not cover.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatIndexMember, HatIndexMembership, HatInstrument
from .corporate_actions import lock_reference_data

__all__ = [
    "COLUMNS",
    "NIFTY_500",
    "Change",
    "IndexMembershipError",
    "Member",
    "load_membership",
    "member_counts",
    "off_size",
    "parse_membership_csv",
    "undo_changes",
]

NIFTY_500 = "NIFTY 500"
COLUMNS = ("index_name", "isin", "symbol_at_the_time", "from_date", "to_date", "source_notice")


class IndexMembershipError(HatError):
    """The history cannot be read or loaded; `code` is stable (`@line` names a CSV line)."""

    def __init__(self, code: str, line: int = 0) -> None:
        super().__init__(code)
        self.code = f"{code}@{line}" if line else code


@dataclass(frozen=True)
class Member:
    """One membership interval [effective_from, effective_to) of `key` (an ISIN in the CSV);
    `effective_to` None: still a member."""

    key: Hashable
    symbol: str
    effective_from: date
    effective_to: date | None
    source: str


@dataclass(frozen=True)
class Change:
    """One notice's change effective on `day` (the first session it holds): `added` joins the
    index and `removed` leaves it, each a key -> the symbol the notice names."""

    day: date
    added: Mapping[Hashable, str]
    removed: Mapping[Hashable, str]
    source: str


def undo_changes(
    current: Mapping[Hashable, str],
    as_of: date,
    changes: Iterable[Change],
    start: date,
    current_source: str,
    conflicts: list[tuple[Change, Hashable]] | None = None,
    anchors: Mapping[date, tuple[Mapping[Hashable, str], str]] | None = None,
) -> list[Member]:
    """The membership intervals from `start` to `as_of`, rebuilt by undoing `changes` (each
    effective after `start` and on or before `as_of`) from `current`, the members on `as_of`
    (key -> symbol), newest first: before a change the members were those after it, less
    `added`, plus `removed`. A change that adds a key not a member after it, removes one that
    still is, or names a key on both sides does not fit the list and is refused
    (`membership_undo_conflict`), as is one outside (start, as_of].

    Given a `conflicts` list, each key that does not fit is appended to it as (change, key) and
    skipped instead, and the rest of the change is undone; and each of `anchors` (an archived
    list: day -> (members before the changes effective that day, its source), after `start`)
    resets the rebuilt members to it once the changes from its day on are undone. A key the
    list differs on is appended as a conflict of the implied change on the anchor's day, which
    the intervals then carry as their source. A history known to be incomplete is reported this
    way, never loaded silently."""
    if anchors and conflicts is None:
        raise ValueError("anchors need a conflicts list")
    state = _Undo(current, current_source, conflicts)
    pending = sorted(
        ((d, a) for d, a in (anchors or {}).items() if start < d <= as_of), reverse=True
    )
    for change in sorted(changes, key=lambda c: c.day, reverse=True):
        if not start < change.day <= as_of:
            raise IndexMembershipError("membership_change_outside_cover")
        while pending and pending[0][0] > change.day:
            day, (listed, source) = pending.pop(0)
            state.reset(day, listed, source)
        state.undo(change)
    for day, (listed, source) in pending:
        state.reset(day, listed, source)
    out = state.out
    for key, symbol in state.members.items():
        end, end_source = state.ends[key]
        out.append(
            _member(key, symbol, start, end, "", end_source)
            if end
            else Member(key, symbol, start, None, current_source)
        )
    return sorted(out, key=lambda m: (m.effective_from, str(m.key)))


class _Undo:
    """The members while `undo_changes` walks back: who is in, when each one's interval ends,
    and the intervals closed so far."""

    def __init__(
        self,
        current: Mapping[Hashable, str],
        current_source: str,
        conflicts: list[tuple[Change, Hashable]] | None,
    ) -> None:
        self.members = dict(current)
        self.ends: dict[Hashable, tuple[date | None, str]] = dict.fromkeys(
            self.members, (None, current_source)
        )
        self.out: list[Member] = []
        self.conflicts = conflicts

    def undo(self, change: Change) -> None:
        members = self.members
        misfit = (
            (change.added.keys() & change.removed.keys())
            | {k for k in change.added if k not in members}
            | {k for k in change.removed if k in members}
        )
        if misfit:
            if self.conflicts is None:
                raise IndexMembershipError("membership_undo_conflict")
            self.conflicts.extend((change, k) for k in sorted(misfit, key=str))
        for key in change.added.keys() - misfit:
            end, end_source = self.ends.pop(key)
            self.out.append(
                _member(key, members.pop(key), change.day, end, change.source, end_source)
            )
        for key in change.removed.keys() - misfit:
            members[key] = change.removed[key]
            self.ends[key] = (change.day, change.source)

    def reset(self, day: date, listed: Mapping[Hashable, str], source: str) -> None:
        """Make the members `listed`, recording each difference as a conflict of the implied
        change on `day`."""
        implied = Change(
            day,
            {k: s for k, s in self.members.items() if k not in listed},
            {k: s for k, s in listed.items() if k not in self.members},
            source,
        )
        if implied.added or implied.removed:
            if self.conflicts is not None:  # always given with anchors
                self.conflicts.extend(
                    (implied, k) for k in sorted([*implied.added, *implied.removed], key=str)
                )
            self.undo(implied)


def _member(
    key: Hashable, symbol: str, start: date, end: date | None, from_source: str, to_source: str
) -> Member:
    source = ";".join(s for s in (from_source, to_source if end is not None else "") if s)
    return Member(key, symbol, start, end, source)


def member_counts(
    rows: Iterable[tuple[date, date | None]], days: Iterable[date]
) -> dict[date, int]:
    """The number of [from, to) intervals containing each of `days`."""
    intervals = list(rows)
    return {
        day: sum(1 for start, end in intervals if start <= day and (end is None or end > day))
        for day in days
    }


def off_size(
    rows: Iterable[tuple[date, date | None]], size: int, covered_to: date
) -> dict[date, int]:
    """The count check: the member count on every day the membership changes (each interval's
    start and end, up to `covered_to`), for the days it is not `size`."""
    intervals = list(rows)
    days = sorted(
        {d for start, end in intervals for d in (start, end) if d is not None and d <= covered_to}
    )
    return {day: n for day, n in member_counts(intervals, days).items() if n != size}


def _date(raw: str, line: int) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise IndexMembershipError("membership_bad_date", line) from None


def parse_membership_csv(text: str) -> tuple[str, list[Member]]:
    """The CSV's index (upper case) and its rows (`COLUMNS`, a header first) as members keyed
    by ISIN. An empty file, a missing column, a bad date, an empty index, ISIN or symbol, an
    interval that does not end after it starts, or rows of more than one index refuses the
    file."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not set(COLUMNS) <= {f.strip() for f in reader.fieldnames}:
        raise IndexMembershipError("membership_csv_columns")
    out: list[Member] = []
    names: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        isin, symbol = row["isin"].upper(), row["symbol_at_the_time"].upper()
        names.add(" ".join(row["index_name"].upper().split()))
        if not isin or not symbol or "" in names:
            raise IndexMembershipError("membership_csv_empty_field", line)
        start = _date(row["from_date"], line)
        end = _date(row["to_date"], line) if row["to_date"] else None
        if end is not None and end <= start:
            raise IndexMembershipError("membership_csv_empty_interval", line)
        out.append(Member(isin, symbol, start, end, row["source_notice"]))
    if not out:
        raise IndexMembershipError("membership_csv_empty")
    if len(names) != 1:
        raise IndexMembershipError("membership_csv_mixed_index")
    return names.pop(), out


def _digest(rows: Iterable[tuple[str, str, date, date | None, str]]) -> str:
    text = "\n".join(
        f"{isin},{symbol},{start},{end or ''},{source}"
        for isin, symbol, start, end, source in sorted(rows, key=_row_key)
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _row_key(row: tuple[Any, str, date, date | None, str]) -> tuple[str, date]:
    return str(row[0]), row[2]


def _overlapping(rows: Iterable[tuple[uuid.UUID, date, date | None]]) -> list[uuid.UUID]:
    """The instruments with two intervals that overlap."""
    by_instrument: dict[uuid.UUID, list[tuple[date, date | None]]] = defaultdict(list)
    for instrument_id, start, end in rows:
        by_instrument[instrument_id].append((start, end))
    found = []
    for instrument_id, spans in by_instrument.items():
        spans.sort()
        if any(end is None or end > nxt for (_, end), (nxt, _) in pairwise(spans)):
            found.append(instrument_id)
    return found


def load_membership(
    session: Session,
    members: Iterable[Member],
    *,
    index_name: str,
    as_of: date,
    size: int,
    source: str,
    apply: bool,
) -> dict[str, Any]:
    """Replace the index's `hat_index_member` rows with `members` (keyed by ISIN) and record
    the source, digest, covered days (the earliest start to `as_of`) and the count check in
    `hat_index_membership`, under the reference-data lock. Without `apply` nothing is written
    (a dry run); loading the same file again changes nothing. Refused: an ISIN no instrument
    has (`membership_unknown_isins`, listed), an interval starting after `as_of`
    (`membership_after_as_of`), or one instrument's intervals overlapping
    (`membership_overlap`)."""
    rows = list(members)
    lock_reference_data(session)
    ids = dict(session.execute(select(HatInstrument.isin, HatInstrument.id)).tuples().all())
    unknown = sorted({str(m.key) for m in rows if m.key not in ids})
    if unknown:
        raise IndexMembershipError("membership_unknown_isins: " + " ".join(unknown))
    if any(m.effective_from > as_of for m in rows):
        raise IndexMembershipError("membership_after_as_of")
    target = {(ids[m.key], m.effective_from): (m.effective_to, m.symbol, m.source) for m in rows}
    if len(target) != len(rows) or _overlapping(
        (i, start, end) for (i, start), (end, _, _) in target.items()
    ):
        raise IndexMembershipError("membership_overlap")
    stored = {
        (r.instrument_id, r.effective_from): r
        for r in session.scalars(
            select(HatIndexMember).where(HatIndexMember.index_name == index_name)
        )
    }
    same = {
        k for k, r in stored.items() if target.get(k) == (r.effective_to, r.symbol, r.source_notice)
    }
    covered_from = min(m.effective_from for m in rows)
    off = off_size(((m.effective_from, m.effective_to) for m in rows), size, as_of)
    coverage = {"size": size, "off_size": {d.isoformat(): n for d, n in sorted(off.items())}}
    digest = _digest(
        (str(m.key), m.symbol, m.effective_from, m.effective_to, m.source) for m in rows
    )
    if apply:
        gone = [k for k in stored if k not in same]
        for instrument_id, start in gone:
            session.execute(
                delete(HatIndexMember).where(
                    HatIndexMember.index_name == index_name,
                    HatIndexMember.instrument_id == instrument_id,
                    HatIndexMember.effective_from == start,
                )
            )
        session.flush()
        session.add_all(
            HatIndexMember(
                index_name=index_name,
                instrument_id=instrument_id,
                effective_from=start,
                effective_to=end,
                symbol=symbol,
                source_notice=notice,
            )
            for (instrument_id, start), (end, symbol, notice) in sorted(target.items())
            if (instrument_id, start) not in same
        )
        record = session.get(HatIndexMembership, index_name) or HatIndexMembership(
            index_name=index_name
        )
        record.source, record.digest, record.rows, record.coverage = (
            source,
            digest,
            len(rows),
            coverage,
        )
        record.covered_from, record.covered_to = covered_from, as_of
        session.add(record)
        session.flush()
    return {
        "applied": apply,
        "index": index_name,
        "source": source,
        "digest": digest,
        "rows": len(rows),
        "instruments": len({i for i, _ in target}),
        "covered_from": covered_from.isoformat(),
        "covered_to": as_of.isoformat(),
        "inserted": len(target) - len(same),
        "deleted": len(stored) - len(same),
        "unchanged": len(same),
        "off_size": coverage["off_size"],
    }
