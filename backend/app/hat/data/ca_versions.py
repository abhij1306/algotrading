"""Versioned point-in-time corporate-action factors (CA6): every value an action ever had, never
overwritten, with the date a backtest could first have used it.

An event is (instrument, ex-date, type). Each write of an action appends a row to
`hat_corporate_action_version` in the write's own transaction (`Ledger`): a load's new row and
its conflicting later values, the owner's corrections and list lines (a delete is a version too),
a dividend amount filled in, a research or file import, CA5's back-fill of `known_on`. A version
carries its `seq` (1, 2, ... per event: the precedence, later wins), its `status`:

- `accepted`: a value the event had; `deleted`: from here on the event has none (the owner
  deleted or retyped it);
- `conflict`: a load's value other than the stored one, kept as evidence and never applied;
- `cross_check`: another estimate of a demerger (`ca_demergers`: the index-neutralised gap, the
  raw gap, the ratio priced at the resulting companies' first close), never applied;
- `disputed`: such an estimate more than `ca_demergers.DISPUTE_TOL` from the applied value, on
  the owner's review list;

its factor and cash, `basis` (CA5's bases for an accepted value; `index_neutral_gap`, `raw_gap`,
`resulting_ratio` for a demerger estimate), source and text, `source_published_on` (a
correction's `known_on`), `first_observed_at` (when HAT first wrote it) and `available_on`: the
first date a backtest could have used the value with the facts it rests on, never before the
ex-date or its evidence (`available_on`), NULL when unknown.

`select(versions, None)` is the hindsight value: the accepted or deleted version of highest
precedence. `hat_corporate_action` stays its projection (one row per event with a value, its
`known_on` the version's `available_on`), so live, the nightly job and the features read what they
read before; `projection_mismatches` proves it. `select(versions, D)` is the value as of D: the
highest precedence among those available on or before D; an unknown date never qualifies. The
backtest keeps one hindsight series and skips the days it is not point in time
(`pending_days`): from the ex-date, every day with no value available yet or with an as-of value
other than the series'. A version added later, available after D, changes nothing on a day up to
D, so a run as of D ignores later facts; only pending days can be added by a back-fill of facts
that were always there.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import select as sql_select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatCorporateActionVersion, HatEvent
from ..money import require_exact

ACCEPTED = "accepted"
DELETED = "deleted"
CONFLICT = "conflict"
CROSS_CHECK = "cross_check"
DISPUTED = "disputed"
STATUSES = (ACCEPTED, DELETED, CONFLICT, CROSS_CHECK, DISPUTED)
SELECTED = (ACCEPTED, DELETED)  # the statuses a selection reads; the rest are evidence

INDEX_NEUTRAL_GAP = "index_neutral_gap"
RAW_GAP = "raw_gap"
RESULTING_RATIO = "resulting_ratio"

SOURCE_OWNER = "owner"  # a value the owner's correction or list wrote

Key = tuple[str, date, str]  # (instrument id, ex-date, type)
Value = tuple[Decimal, Decimal | None]  # (factor, cash)


@dataclass(frozen=True)
class Version:
    """One version as the selection reads it."""

    seq: int
    status: str
    factor: Decimal
    cash: Decimal | None = None
    available_on: date | None = None
    basis: str | None = None

    @property
    def value(self) -> Value:
        return (self.factor, self.cash)


def available_on(ex_date: date, evidence: Sequence[date | None]) -> date | None:
    """The first date a value resting on `evidence` (the dates of the prices and sources it and
    its acceptance read) could have been used: the latest of them, never before the ex-date;
    None (unknown) when there is no evidence or one date is unknown."""
    if not evidence or any(d is None for d in evidence):
        return None
    return max(ex_date, *(d for d in evidence if d is not None))


def select(versions: Iterable[Version], as_of: date | None) -> Version | None:
    """The event's accepted version of highest precedence, None when it has none or the top one
    is a delete. `as_of` None is hindsight (every version); a date reads only the versions
    available on or before it (an unknown date never qualifies)."""
    found = [
        v
        for v in versions
        if v.status in SELECTED
        and (as_of is None or (v.available_on is not None and v.available_on <= as_of))
    ]
    if not found:
        return None
    top = max(found, key=lambda v: v.seq)
    return None if top.status == DELETED else top


def pending_days(
    versions: Sequence[Version], ex_date: date, days: Sequence[date], applied: Value
) -> list[date]:
    """The sessions of the sorted `days`, from the ex-date on, a series applying `applied` is not
    point in time: no version available yet, or the as-of value differs from `applied`. The
    selection changes only on an `available_on`, so it is read once per span."""
    marks = sorted(
        {
            v.available_on
            for v in versions
            if v.status in SELECTED and v.available_on is not None and v.available_on > ex_date
        }
    )
    bounds = [ex_date, *marks]
    found: list[date] = []
    for k, start in enumerate(bounds):
        now = select(versions, start)
        if now is not None and now.value == applied:
            continue
        stop = len(days) if k + 1 == len(bounds) else bisect_left(days, bounds[k + 1])
        found.extend(days[bisect_left(days, start) : stop])
    return found


def band(estimates: Iterable[Fraction]) -> tuple[Fraction, Fraction]:
    """A demerger's band: the range of its estimates (not a confidence interval: the stock's own
    news that morning is in every open-based estimate)."""
    found = list(estimates)
    return min(found), max(found)


def log_apart(a: Fraction, b: Fraction) -> float:
    """How far apart two factors are, in log terms."""
    return abs(math.log(a / b))


# --- the ledger -----------------------------------------------------------------------------------


def _key(instrument_id: object, ex_date: date, action_type: str) -> Key:
    return (str(instrument_id), ex_date, action_type)


def _cash(raw: object) -> Decimal | None:
    return None if raw is None else require_exact(raw, "cash")


@dataclass
class Ledger:
    """Appends versions in the caller's transaction, under the reference-data lock its writer
    holds: `seq` follows the event's stored versions. Built for the instruments a write touches
    (`open`)."""

    session: Session
    seqs: dict[Key, int] = field(default_factory=dict)
    seen: set[tuple[Key, str, Decimal, Decimal | None, str | None, date | None]] = field(
        default_factory=set
    )

    @classmethod
    def open(cls, session: Session, instrument_ids: Iterable[object]) -> Ledger:
        ledger = cls(session)
        ids = {uuid.UUID(str(i)) for i in instrument_ids}
        if not ids:
            return ledger
        for v in session.scalars(
            sql_select(HatCorporateActionVersion).where(
                HatCorporateActionVersion.instrument_id.in_(ids)
            )
        ):
            key = _key(v.instrument_id, v.ex_date, v.action_type)
            ledger.seqs[key] = max(ledger.seqs.get(key, 0), v.seq)
            ledger.seen.add(
                (key, v.status, Decimal(v.factor), _cash(v.cash), v.basis, v.available_on)
            )
        return ledger

    def has(
        self,
        key: Key,
        status: str,
        factor: Decimal,
        cash: Decimal | None = None,
        basis: str | None = None,
        available: date | None = None,
    ) -> bool:
        """Is this exact version stored already (a rerun appends nothing)?"""
        return (key, status, factor, cash, basis, available) in self.seen

    def has_status(self, key: Key, status: str, factor: Decimal, cash: Decimal | None) -> bool:
        """Is a version of this status and value stored, whatever its basis and date?"""
        return any(s[:4] == (key, status, factor, cash) for s in self.seen if s[0] == key)

    def add(
        self,
        key: Key,
        *,
        status: str,
        factor: Decimal,
        cash: Decimal | None,
        available: date | None,
        basis: str | None,
        source: str,
        text: str,
        published_on: date | None = None,
        observed_at: datetime | None = None,
        band_range: tuple[Fraction, Fraction] | None = None,
    ) -> int:
        """Append one version; returns its seq."""
        seq = self.seqs.get(key, 0) + 1
        self.seqs[key] = seq
        self.seen.add((key, status, factor, cash, basis, available))
        row = HatCorporateActionVersion(
            instrument_id=uuid.UUID(key[0]),
            ex_date=key[1],
            action_type=key[2],
            seq=seq,
            status=status,
            factor=factor,
            cash=cash,
            basis=basis,
            source=source,
            text=text[:500],
            source_published_on=published_on,
            available_on=available,
            band_low=None if band_range is None else _decimal(band_range[0]),
            band_high=None if band_range is None else _decimal(band_range[1]),
        )
        if observed_at is not None:
            row.first_observed_at = observed_at
        self.session.add(row)
        return seq

    def add_row(
        self,
        row: HatCorporateAction,
        *,
        source: str | None = None,
        text: str | None = None,
        published_on: date | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        """Append a stored row's value as the event's accepted version (`row` as just written)."""
        return self.add(
            _key(row.instrument_id, row.ex_date, row.action_type),
            status=ACCEPTED,
            factor=Decimal(row.adjustment_factor),
            cash=_cash(row.cash_amount),
            available=row.known_on,
            basis=row.known_basis,
            source=source or row.source,
            text=row.raw_text if text is None else text,
            published_on=published_on,
            observed_at=observed_at,
        )

    def delete(self, row: HatCorporateAction, *, available: date | None, text: str) -> int:
        """Append the owner's delete of a stored row's event (a delete, a retype away)."""
        return self.add(
            _key(row.instrument_id, row.ex_date, row.action_type),
            status=DELETED,
            factor=Decimal(row.adjustment_factor),
            cash=_cash(row.cash_amount),
            available=available,
            basis=None,
            source=SOURCE_OWNER,
            text=text,
        )


def _decimal(value: Fraction) -> Decimal:
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("1e-12"))


# --- reading ----------------------------------------------------------------------------------


def read_versions(
    session: Session, instrument_ids: Iterable[object] | None = None
) -> dict[Key, list[Version]]:
    """Every stored version, by event (of `instrument_ids` when given), in seq order."""
    query = sql_select(HatCorporateActionVersion).order_by(HatCorporateActionVersion.seq)
    if instrument_ids is not None:
        ids = {uuid.UUID(str(i)) for i in instrument_ids}
        query = query.where(HatCorporateActionVersion.instrument_id.in_(ids))
    found: dict[Key, list[Version]] = defaultdict(list)
    for v in session.scalars(query):
        found[_key(v.instrument_id, v.ex_date, v.action_type)].append(
            Version(v.seq, v.status, Decimal(v.factor), _cash(v.cash), v.available_on, v.basis)
        )
    return found


def digest(session: Session) -> tuple[str, int]:
    """(sha256 of every version, how many): what a point-in-time run read."""
    h = hashlib.sha256()
    count = 0
    query = sql_select(
        HatCorporateActionVersion.instrument_id,
        HatCorporateActionVersion.ex_date,
        HatCorporateActionVersion.action_type,
        HatCorporateActionVersion.seq,
        HatCorporateActionVersion.status,
        HatCorporateActionVersion.factor,
        HatCorporateActionVersion.cash,
        HatCorporateActionVersion.available_on,
    ).order_by(
        HatCorporateActionVersion.instrument_id,
        HatCorporateActionVersion.ex_date,
        HatCorporateActionVersion.action_type,
        HatCorporateActionVersion.seq,
    )
    for row in session.execute(query):
        iid, ex, kind, seq, status, factor, cash, available = row
        cash_text = "" if cash is None else str(Decimal(cash).normalize())
        h.update(
            f"{iid}|{ex}|{kind}|{seq}|{status}|{Decimal(factor).normalize()}|{cash_text}"
            f"|{available or ''}\n".encode()
        )
        count += 1
    return h.hexdigest(), count


@dataclass(frozen=True)
class Stored:
    """A stored row, as the projection check and the back-fill read it."""

    key: Key
    factor: Decimal
    cash: Decimal | None
    known_on: date | None
    basis: str | None
    source: str
    text: str


def stored_rows(session: Session) -> list[Stored]:
    return [
        Stored(
            _key(a.instrument_id, a.ex_date, a.action_type),
            Decimal(a.adjustment_factor),
            _cash(a.cash_amount),
            a.known_on,
            a.known_basis,
            a.source,
            a.raw_text,
        )
        for a in session.scalars(sql_select(HatCorporateAction))
    ]


def projection_mismatches(
    rows: Sequence[Stored], versions: Mapping[Key, Sequence[Version]]
) -> list[tuple[Key, str]]:
    """Each event where `hat_corporate_action` is not the hindsight selection: a stored row with
    no version, another value, date or basis, or an event whose hindsight value has no row."""
    found: list[tuple[Key, str]] = []
    stored = {r.key: r for r in rows}
    for key, row in stored.items():
        top = select(versions.get(key, ()), None)
        if top is None:
            found.append((key, "no_version" if key not in versions else "deleted_but_stored"))
        elif top.value != (row.factor, row.cash):
            found.append((key, "value"))
        elif (top.available_on, top.basis) != (row.known_on, row.basis):
            found.append((key, "available_on"))
    for key, vs in versions.items():
        if key not in stored and select(vs, None) is not None:
            found.append((key, "not_stored"))
    return found


# --- the back-fill ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    """One `corporate_action.corrected` journal entry."""

    at: datetime
    instrument_id: str
    ex_date: date
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    note: str


@dataclass(frozen=True)
class Planned:
    """A version the back-fill appends."""

    key: Key
    status: str
    factor: Decimal
    cash: Decimal | None
    available: date | None
    basis: str | None
    source: str
    text: str
    observed_at: datetime | None  # None: now
    origin: str  # "row" (a stored row), "before" (a journalled before state), "delete"


def plan_backfill(
    rows: Sequence[Stored],
    corrections: Sequence[Correction],
    imported: Mapping[Key, tuple[datetime, str]],
    versioned: set[Key],
) -> list[Planned]:
    """The versions of every event with none yet (not in `versioned`): each journalled `before`
    state of an owner's correction (its date unknown: it was stored before CA5 and no journal
    kept one) and a delete where the correction vacated the key, then the stored row (its
    `known_on`). `first_observed_at` is the journal's: the import (`imported`: the first
    `corporate_action.imported` of the event and its source) or the correction that wrote the
    value, else None (now)."""
    writer: dict[Key, tuple[datetime, str]] = dict(imported)
    planned: list[Planned] = []
    for c in sorted(corrections, key=lambda c: c.at):
        before_type = str(c.before.get("type", ""))
        key = (c.instrument_id, c.ex_date, before_type)
        if key not in versioned and before_type and not c.before.get("absent"):
            factor, cash = require_exact(c.before["factor"], "factor"), _cash(c.before.get("cash"))
            at, source = writer.get(key, (c.at, "unknown"))
            planned.append(
                Planned(
                    key, ACCEPTED, factor, cash, None, None, source, "journal before", at, "before"
                )
            )
            if (
                c.after.get("deleted")
                or c.after.get("kept_stored")
                or (c.after.get("type") != before_type)
            ):
                planned.append(
                    Planned(
                        key, DELETED, factor, cash, None, None, SOURCE_OWNER, c.note, c.at, "delete"
                    )
                )
        written = c.after.get("type")
        if written and not c.after.get("deleted") and not c.after.get("kept_stored"):
            writer[(c.instrument_id, c.ex_date, str(written))] = (c.at, SOURCE_OWNER)
    for r in rows:
        if r.key in versioned:
            continue
        at = writer.get(r.key, (None, ""))[0]
        planned.append(
            Planned(
                r.key, ACCEPTED, r.factor, r.cash, r.known_on, r.basis, r.source, r.text, at, "row"
            )
        )
    return planned


def journal(session: Session) -> tuple[list[Correction], dict[Key, tuple[datetime, str]]]:
    """The owner's corrections and each event's first import, from the journal."""
    from .ca_import import EVENT_IMPORTED
    from .corporate_actions import EVENT_CORRECTED

    corrections = []
    imported: dict[Key, tuple[datetime, str]] = {}
    for e in session.scalars(
        sql_select(HatEvent)
        .where(HatEvent.event_type.in_((EVENT_CORRECTED, EVENT_IMPORTED)))
        .order_by(HatEvent.occurred_at)
    ):
        p = e.payload
        if e.event_type == EVENT_CORRECTED:
            corrections.append(
                Correction(
                    e.occurred_at,
                    str(e.aggregate_id),
                    date.fromisoformat(p["ex_date"]),
                    p.get("before", {}),
                    p.get("after", {}),
                    str(p.get("note", "")),
                )
            )
        elif "type" in p and "ex_date" in p:
            key = (str(e.aggregate_id), date.fromisoformat(p["ex_date"]), str(p["type"]))
            imported.setdefault(key, (e.occurred_at, str(p.get("source", "unknown"))))
    return corrections, imported


def versioned_keys(session: Session) -> set[Key]:
    """The events with an accepted or deleted version (a conflict alone, appended by a load
    before the back-fill ran, leaves its event to the back-fill)."""
    return {
        _key(i, ex, kind)
        for i, ex, kind in session.execute(
            sql_select(
                HatCorporateActionVersion.instrument_id,
                HatCorporateActionVersion.ex_date,
                HatCorporateActionVersion.action_type,
            )
            .where(HatCorporateActionVersion.status.in_(SELECTED))
            .distinct()
        )
    }


def apply_backfill(session: Session, planned: Sequence[Planned]) -> int:
    """Append the planned versions (the caller holds the reference-data lock)."""
    ledger = Ledger.open(session, {p.key[0] for p in planned})
    for p in planned:
        ledger.add(
            p.key,
            status=p.status,
            factor=p.factor,
            cash=p.cash,
            available=p.available,
            basis=p.basis,
            source=p.source,
            text=p.text,
            observed_at=p.observed_at,
        )
    session.flush()
    return len(planned)
