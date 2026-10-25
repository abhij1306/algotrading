"""The shared apply step of the price-changing corporate-action imports (CA1, CA2): what a row
proposes (`Proposal`, its `Leg`s), its plan against the actions stored near its ex-date
(`settle`: store, already, conflict or review), the file's self-contradictions (`dedupe`), the
keys the owner vacated (`drop_vacated`), the journalled write (`apply`), the report and the
command-line input guard. The file import (`ca_import`) and the research import (`ca_research`)
are adapters that turn their inputs into proposals and outcomes; the gate and the raw-bar
windows are `ca_gate`'s. The dividend imports (`ca_payouts`) write through `store_new` and
`journal_imported` too.

A confirmed factor is stored as a new row of its own type on the instrument that holds the
history before the ex-date, through `load_actions` (a stored key is never rewritten: a different
factor is a conflict, fixed with `correct_action`; a key the owner vacated is never re-created),
and journalled `corporate_action.imported` in the same transaction. An action already stored
within `NEAR_DAYS` under the same company's other ISIN, symbol or lineage chain is not stored
again.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..models import HatCorporateAction
from .ca_gate import (
    ALREADY,
    CONFLICT,
    EVENT_IMPORTED,
    REVIEW,
    SKIPPED,
    STATUSES,
    STORED,
    Bar,
    CaFileError,
    Reference,
    Verdict,
    fmt4,
    same_factor,
)
from .ca_known import UNKNOWN, Known
from .corporate_actions import (
    PRICE_NEUTRAL_TYPES,
    ActionRecord,
    factor_to_decimal,
    load_actions,
    lock_reference_data,
    owner_vacated,
)
from .store import FACTOR_MIN

NEAR_DAYS = 5  # a stored action this close to the ex-date is the same event (sources differ)
KIND_DEMERGER = "demerger"  # a stored `other` row that is a demerger (`Stored.kind`)


@dataclass(frozen=True)
class Leg:
    """One factor a row proposes, of a stored type (an `other` that is a demerger flagged)."""

    action_type: str
    factor: Fraction
    demerger: bool = False


@dataclass(frozen=True)
class RightsTerms:
    """`new` shares for every `held`, issued at `price` (face value plus premium)."""

    new: Fraction
    held: Fraction
    price: Fraction


@dataclass(frozen=True)
class Proposal:
    """One file row, as far as the text alone decides it. `legs` is empty for a rights issue or a
    demerger, whose factor needs the bars; `status`/`reason` are set when the row ends here."""

    table: str
    rowid: str
    symbol: str
    isin: str
    ex_date: date | None
    text: str
    hint: str = ""
    legs: tuple[Leg, ...] = ()
    note: str = ""  # a demerger's curated ratio note: names the resulting company
    rights: RightsTerms | None = None
    demerger: tuple[Fraction, Fraction] | None = None
    sme: bool = False
    status: str = ""
    reason: str = ""

    def ends(self, status: str, reason: str) -> Proposal:
        return replace(self, status=status, reason=reason)


@dataclass(frozen=True)
class Stored:
    instrument_id: str
    ex_date: date
    action_type: str
    factor: Decimal
    demerger: bool = False

    @property
    def kind(self) -> str:
        """The type, with a demerger told apart from the other `other` rows (rights)."""
        return KIND_DEMERGER if self.demerger else self.action_type


@dataclass
class Outcome:
    proposal: Proposal
    status: str
    reason: str = ""
    detail: str = ""
    records: list[ActionRecord] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def plan_legs(
    legs: Sequence[Leg], near: Sequence[Stored]
) -> tuple[list[Leg], list[Leg], list[tuple[Leg, Stored]]]:
    """Each leg against the price-changing actions stored near its ex-date for the same company:
    to store (none of its type), already stored (the same factor), or a conflict (another)."""
    new, already, conflicts = [], [], []
    for leg in legs:
        same = [s for s in near if s.action_type == leg.action_type]
        if not same:
            new.append(leg)
        elif any(same_factor(s.factor, leg.factor) for s in same):
            already.append(leg)
        else:
            conflicts.append((leg, same[0]))
    return new, already, conflicts


@dataclass
class Context:
    reference: Reference
    stored: dict[str, list[Stored]]  # instrument -> its price-changing actions
    first_closes: dict[str, tuple[date, Decimal]]


def near_actions(ctx: Context, ids: Iterable[str], ex_date: date) -> list[Stored]:
    seen: dict[tuple[date, str, Decimal], Stored] = {}
    for iid in ids:
        for s in ctx.stored.get(iid, ()):
            if abs((s.ex_date - ex_date).days) <= NEAR_DAYS:
                seen.setdefault((s.ex_date, s.action_type, s.factor), s)  # chain copies once
    return sorted(seen.values(), key=lambda s: (s.ex_date, s.action_type, s.instrument_id))


def settle(
    p: Proposal,
    before: Bar,
    legs: Sequence[Leg],
    near: Sequence[Stored],
    verdict: Verdict,
    detail: str,
    evidence: dict[str, Any],
    *,
    source: str,
    accept: frozenset[str] = frozenset(),
    known: Known = UNKNOWN,
) -> Outcome:
    """The plan of `legs` against the same-kind actions stored `near` the ex-date, given the
    gate's verdict: store, already, conflict or review. A verdict whose reason is in `accept`
    still stores (the research import's rights, `price_unconfirmed`); the row goes on the
    instrument of the bar `before` the ex-date, known on `known` (CA5)."""
    new, already, conflicts = plan_legs(legs, near)
    if conflicts:
        text = "; ".join(
            f"{leg.action_type} file {fmt4(leg.factor)} vs stored {s.factor} on {s.ex_date}"
            for leg, s in conflicts
        )
        bars_say = "the bars confirm the file" if verdict.confirmed else f"file {verdict.reason}"
        return Outcome(p, CONFLICT, "stored_factor_differs", f"{detail}; {text}; {bars_say}")
    if not new:
        return Outcome(p, ALREADY, "", detail)
    if not verdict.confirmed and verdict.reason not in accept:
        return Outcome(p, REVIEW, verdict.reason, detail)
    if p.ex_date is None or any(not FACTOR_MIN <= factor_to_decimal(leg.factor) < 1 for leg in new):
        return Outcome(p, REVIEW, "factor_out_of_range", detail)
    records = [
        ActionRecord(
            uuid.UUID(before.instrument_id),
            p.ex_date,
            leg.action_type,
            factor_to_decimal(leg.factor),
            p.text,
            source,
            known=known,
            demerger=leg.demerger,
        )
        for leg in new
    ]
    reason = "other legs stored" if already else verdict.reason
    return Outcome(p, STORED, reason, detail, records, evidence)


def dedupe(outcomes: list[Outcome]) -> None:
    """Two rows of the file (NSE keeps revisions as separate rows) that store the same key: the
    same factor is stored once, by the first row (a later row keeps its other legs); different
    factors for the key put every row naming it in review, since the file contradicts itself."""
    by_key: dict[tuple, list[tuple[Outcome, ActionRecord]]] = defaultdict(list)
    for o in outcomes:
        for r in o.records:
            by_key[r.key].append((o, r))
    disagree: set[int] = set()
    for group in by_key.values():
        if len({r.factor for _, r in group}) > 1:
            disagree.update(id(o) for o, _ in group)
        else:
            for o, r in group[1:]:
                o.records = [kept for kept in o.records if kept is not r]
    for o in outcomes:
        if id(o) in disagree:
            o.status, o.reason, o.records = REVIEW, "file_disagrees_with_itself", []
        elif o.status == STORED and not o.records:
            o.status, o.reason = ALREADY, "duplicate_in_file"


def stored_price_actions(session: Session) -> dict[str, list[Stored]]:
    out: dict[str, list[Stored]] = defaultdict(list)
    for row in session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES))
    ):
        iid = str(row.instrument_id)
        out[iid].append(
            Stored(iid, row.ex_date, row.action_type, Decimal(row.adjustment_factor), row.demerger)
        )
    return out


def drop_vacated(session: Session, outcomes: Sequence[Outcome]) -> None:
    """An outcome that would store a key the owner vacated stores nothing (`owner_vacated`)."""
    held = owner_vacated(session, {r.instrument_id for o in outcomes for r in o.records})
    for o in outcomes:
        if any(r.key in held for r in o.records):
            o.status, o.reason, o.records = SKIPPED, "owner_vacated", []


class ImportRow(Protocol):
    """The input row an imported record came from, as the journal names it."""

    @property
    def table(self) -> str: ...
    @property
    def rowid(self) -> str: ...
    @property
    def symbol(self) -> str: ...
    @property
    def isin(self) -> str: ...


def store_new(session: Session, records: Sequence[ActionRecord]) -> int:
    """Store `records` as new rows (`load_actions`) under the reference-data lock; returns how
    many. Raises `ca_import_raced` when the database disagrees with the plan (a row appeared
    meanwhile): the caller's transaction then keeps nothing."""
    lock_reference_data(session)
    report = load_actions(session, records)
    if report.created != len(records) or report.conflicts or report.held:
        raise CaFileError("ca_import_raced")
    return report.created


def journal_imported(
    session: Session,
    instrument_id: object,
    row: ImportRow,
    values: Mapping[str, Any],
    *,
    journal: Mapping[str, Any],
    evidence: Mapping[str, Any],
    now: datetime,
) -> None:
    """One `corporate_action.imported` entry: `journal` (the run id, the source and the input
    files' digests), the input row, the stored `values` (`text` cut to 500) and the check."""
    events.record(
        session,
        aggregate_type="instrument",
        aggregate_id=instrument_id,  # type: ignore[arg-type]
        event_type=EVENT_IMPORTED,
        actor="engine",
        payload={
            **journal,
            "table": row.table,
            "row": row.rowid,
            "symbol": row.symbol,
            "isin": row.isin,
            **values,
            "text": str(values.get("text", ""))[:500],
            "check": evidence,
        },
        occurred_at=now,
    )


def apply(
    session: Session, outcomes: Sequence[Outcome], *, journal: Mapping[str, Any], now: datetime
) -> int:
    """Store every confirmed record and journal each new row as `corporate_action.imported` in
    the same transaction, `journal` (the source and the input files' digests) in every payload;
    returns the rows created (`store_new`: nothing is kept when the database disagrees)."""
    created = store_new(session, [r for o in outcomes for r in o.records])
    journal = {**journal, "run_id": str(uuid.uuid4())}
    for o in outcomes:
        for r in o.records:
            values = {
                "ex_date": r.ex_date.isoformat(),
                "type": r.action_type,
                "factor": str(r.factor),
                "text": r.raw_text,
            }
            journal_imported(
                session,
                r.instrument_id,
                o.proposal,
                values,
                journal=journal,
                evidence=o.evidence,
                now=now,
            )
    session.flush()
    return created


def report_lines(
    outcomes: Sequence[Outcome],
    *,
    applied: bool,
    everything: bool,
    tables: Sequence[str],
) -> list[str]:
    """Counts per table and status, then the stored, conflict and review rows with their
    reasons (every row when `everything`)."""
    counts: Counter[tuple[str, str]] = Counter((o.proposal.table, o.status) for o in outcomes)
    rows = Counter(r.action_type for o in outcomes for r in o.records)
    label = {STORED: "stored" if applied else "to_store"}
    lines = [
        "table               " + " ".join(f"{label.get(s, s):>9}" for s in STATUSES) + "    total",
    ]
    for table in tables:
        cells = [counts[(table, s)] for s in STATUSES]
        lines.append(f"{table:<20}" + " ".join(f"{c:>9}" for c in cells) + f" {sum(cells):>8}")
    lines.append(
        "rows "
        + ("written" if applied else "to write")
        + ": "
        + ", ".join(f"{k} {v}" for k, v in sorted(rows.items()))
    )
    for table in tables:
        reasons = Counter(
            (o.status, o.reason)
            for o in outcomes
            if o.proposal.table == table and o.status in (REVIEW, SKIPPED)
        )
        lines.append(
            f"reasons {table}: "
            + ", ".join(f"{s}/{r} {n}" for (s, r), n in sorted(reasons.items()))
        )
    shown = STATUSES if everything else (STORED, CONFLICT, REVIEW)
    for status in shown:
        for o in outcomes:
            if o.status != status:
                continue
            p = o.proposal
            factors = " ".join(f"{r.action_type}={r.factor.normalize()}" for r in o.records)
            lines.append(
                "\t".join(
                    [
                        label.get(o.status, o.status).upper(),
                        o.reason,
                        p.table,
                        p.symbol,
                        p.isin,
                        p.ex_date.isoformat() if p.ex_date else "-",
                        factors,
                        p.text[:120],
                        o.detail,
                        p.hint[:160],
                    ]
                )
            )
    return lines


# --- command line -------------------------------------------------------------------------------


def input_path(name: str, suffix: str = "", *, directory: bool = False) -> Path:
    """A path named on the command line, under the working directory (a file with `suffix`, or a
    directory): anything resolving outside it, or with another suffix, is refused before it is
    opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or (not directory and target.suffix.lower() != suffix):
        raise CaFileError("ca_file_outside_cwd")
    if not (target.is_dir() if directory else target.is_file()):
        raise CaFileError("ca_file_missing")
    return target


def digest(path: Path) -> str:
    """The file's sha256, as the import journal records it."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
