"""The owner's verdict on a found ISIN succession (`link` / `unlink OLD NEW`, D1): kept in
`hat_lineage_override`, applied over the rules' verdicts at once and at every build
(`lineage.apply_overrides`), and journalled `lineage.overridden`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatInstrument, HatIsinLineage, HatLineageOverride
from .chains import LINKED, LineageError, chains
from .corporate_actions import lock_reference_data
from .lineage import LINK, UNLINK, Decision, apply_overrides, stored_overrides
from .lineage_store import BuildReport, save_decisions

EVENT_OVERRIDDEN = "lineage.overridden"
NOTE_MAX = 500


def override_lineage(
    session: Session,
    old_isin: str,
    new_isin: str,
    verdict: str,
    *,
    note: str,
    owner: tuple[int, uuid.UUID, datetime],
) -> dict[str, Any]:
    """Store the owner's `link` or `unlink` of a found succession (old ISIN -> new ISIN), apply
    it to the stored verdicts at once (`apply_overrides`) and journal `lineage.overridden`
    (actor user, via cli), in the caller's transaction, under the reference-data lock.
    `owner` is (chat id, user id, now).
    Refused, writing nothing: a bad verdict or note (`lineage_bad_override`), an ISIN with no
    instrument (`lineage_unknown_isin`), a pair no build found (`lineage_not_a_succession`) and
    a link that would fork a chain another owner link holds (`lineage_fork`). A rules link it
    displaces comes back at the next build once the owner's link is undone."""
    if verdict not in (LINK, UNLINK) or not note.strip() or len(note) > NOTE_MAX:
        raise LineageError("lineage_bad_override")
    lock_reference_data(session)
    ids = {
        i.isin: i.id
        for i in session.scalars(
            select(HatInstrument).where(HatInstrument.isin.in_((old_isin, new_isin)))
        )
    }
    if old_isin not in ids or new_isin not in ids:
        raise LineageError("lineage_unknown_isin")
    pair = (str(ids[old_isin]), str(ids[new_isin]))
    rows = {
        (str(r.old_instrument_id), str(r.new_instrument_id)): r
        for r in session.scalars(select(HatIsinLineage))
    }
    if pair not in rows:
        raise LineageError("lineage_not_a_succession")
    before = f"{rows[pair].status}:{rows[pair].reason}"
    stored = [
        Decision(old, new, r.effective_date, r.status, r.reason, dict(r.evidence), r.known_on)
        for (old, new), r in rows.items()
    ]
    decisions = apply_overrides(stored, {**stored_overrides(session), pair: verdict})
    chains((d.old_id, d.new_id) for d in decisions if d.status == LINKED)  # never a fork
    chat_id, user_id, now = owner
    row = session.scalar(
        select(HatLineageOverride).where(
            HatLineageOverride.old_instrument_id == ids[old_isin],
            HatLineageOverride.new_instrument_id == ids[new_isin],
        )
    )
    if row is None:
        row = HatLineageOverride(old_instrument_id=ids[old_isin], new_instrument_id=ids[new_isin])
        session.add(row)
    row.verdict, row.note, row.decided_at = verdict, note.strip(), now
    report = BuildReport()
    save_decisions(session, decisions, report)
    payload = {
        "chat_id": chat_id,
        "via": "cli",
        "old_isin": old_isin,
        "new_isin": new_isin,
        "verdict": verdict,
        "before": before,
        "note": note.strip(),
    }
    record(
        session,
        aggregate_type="instrument",
        aggregate_id=ids[old_isin],
        event_type=EVENT_OVERRIDDEN,
        actor="user",
        payload=payload,
        user_id=user_id,
        occurred_at=now,
    )
    session.flush()
    changed = report.updated
    return {**{k: v for k, v in payload.items() if k != "chat_id"}, "rows_changed": changed}
