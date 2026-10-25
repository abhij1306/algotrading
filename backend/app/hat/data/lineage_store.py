"""The ISIN lineage verdicts in the database: `save_decisions` makes `hat_isin_lineage` hold a
build's decisions (`lineage.build_lineage`, the owner's `lineage_override`) and journals each pair
that gains or loses its link (`lineage.changed`), since that changes every adjusted series of its
chain and the `position.isin_changed` lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatIsinLineage
from .chains import LINKED, REVIEW

if TYPE_CHECKING:
    from .lineage import Decision

EVENT_CHANGED = "lineage.changed"  # a pair gained or lost its link (`save_decisions`)


@dataclass
class BuildReport:
    linked: int = 0
    review: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    unknown: int = 0  # candidates naming an instrument id the database does not have
    reasons: dict[str, int] = field(default_factory=dict)
    new_review: list[str] = field(default_factory=list)  # "OLDISIN>NEWISIN", new on the list


Pair = tuple[str, str]  # (old instrument id, new instrument id)


def link_changes(
    before: Mapping[Pair, str], after: Mapping[Pair, str]
) -> list[tuple[Pair, str | None, str | None]]:
    """Each pair whose verdict crosses `linked` (it gains or loses its link), with its status
    before and after (None: no row), in pair order. A change between other verdicts, or of a
    link's evidence, changes no adjusted series and is not listed."""
    return [
        (pair, before.get(pair), after.get(pair))
        for pair in sorted(before.keys() | after.keys())
        if (before.get(pair) == LINKED) != (after.get(pair) == LINKED)
    ]


def save_decisions(session: Session, decisions: Sequence[Decision], report: BuildReport) -> None:
    """Make `hat_isin_lineage` hold exactly `decisions`: a pair's row is created, updated when
    its verdict or evidence changed, left alone when not, and a stored pair no longer found is
    deleted. Deletes run first, so a pair that changes sides never trips the one-link index. Each
    pair that gains or loses its link (`link_changes`) is journalled `lineage.changed` in the
    same transaction: it changes every adjusted series of its chain."""
    stored = {
        (str(r.old_instrument_id), str(r.new_instrument_id)): r
        for r in session.scalars(select(HatIsinLineage))
    }
    wanted = {(d.old_id, d.new_id): d for d in decisions}
    _journal_links(session, stored, wanted)
    for key, row in stored.items():
        if key not in wanted:
            session.delete(row)
            report.removed += 1
    session.flush()
    # a link demoted first, then the rest: the partial unique indexes see no transient fork
    ordered = sorted(wanted.values(), key=lambda d: d.status == LINKED)
    for decision in ordered:
        row = stored.get((decision.old_id, decision.new_id))
        values = {
            "effective_date": decision.effective,
            "status": decision.status,
            "reason": decision.reason,
            "evidence": decision.evidence,
            "known_on": decision.known_on,
        }
        if decision.status == REVIEW and (row is None or row.status != REVIEW):
            evidence = decision.evidence
            report.new_review.append(f"{evidence['old_isin']}>{evidence['new_isin']}")
        if row is None:
            session.add(
                HatIsinLineage(
                    old_instrument_id=uuid.UUID(decision.old_id),
                    new_instrument_id=uuid.UUID(decision.new_id),
                    **values,
                )
            )
            report.created += 1
        elif all(getattr(row, k) == v for k, v in values.items()):
            report.unchanged += 1
        else:
            for k, v in values.items():
                setattr(row, k, v)
            report.updated += 1
        session.flush()


def _journal_links(
    session: Session, stored: Mapping[Pair, HatIsinLineage], wanted: Mapping[Pair, Decision]
) -> None:
    before = {pair: row.status for pair, row in stored.items()}
    after = {pair: d.status for pair, d in wanted.items()}
    for pair, was, now in link_changes(before, after):
        row, decision = stored.get(pair), wanted.get(pair)
        evidence = row.evidence if row is not None else {}
        if decision is not None:
            evidence = decision.evidence
        record(
            session,
            aggregate_type="instrument",
            aggregate_id=uuid.UUID(pair[0]),
            event_type=EVENT_CHANGED,
            actor="engine",
            payload={
                "old_isin": evidence.get("old_isin"),
                "new_isin": evidence.get("new_isin"),
                "new_instrument_id": pair[1],
                "before": was,
                "after": now,
                "reason": decision.reason if decision is not None else "not_found",
            },
        )
