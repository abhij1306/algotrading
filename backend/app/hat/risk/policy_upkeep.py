"""Risk-policy reads and startup upkeep that need no backtest or order code (HAT-RISK-011).

`app.database` runs `ensure_hat_policies` at startup, and the Telegram text path imports
`app.database`, so this module imports only the models, the event log and `risk.policy`
(CONF `telegram.no_order_import_in_text_path`). `policy_service` builds on it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatRiskPolicyVersion
from ..time import as_utc, utc_now
from .policy import (
    OLD_SEED_POLICY,
    SEED_POLICY,
    PolicyVersion,
    effective_version,
    parse_policy,
)


def load_versions(session: Session, user_id: uuid.UUID) -> list[PolicyVersion]:
    """Every stored version of the user's policy, parsed (a malformed stored document raises)."""
    rows = session.scalars(
        select(HatRiskPolicyVersion).where(HatRiskPolicyVersion.user_id == user_id)
    ).all()
    return [
        PolicyVersion(r.id, r.version, r.effective_from, r.superseded_at, parse_policy(r.policy))
        for r in rows
    ]


EVENT_REQUESTED = "risk.change_requested"
EVENT_EFFECTIVE = "risk.change_effective"


def record_policy_event(
    session: Session,
    row: HatRiskPolicyVersion,
    kind: str,
    actor: str,
    chat_id: int | None,
    at: datetime,
) -> None:
    payload: dict[str, Any] = {"version": row.version, "direction": row.direction}
    if actor == "user":
        payload["chat_id"] = chat_id
    record(
        session,
        aggregate_type="risk_policy",
        aggregate_id=row.id,
        event_type=kind,
        actor=actor,
        payload=payload,
        user_id=row.user_id,
        occurred_at=at,
    )


def next_version(session: Session, user_id: uuid.UUID) -> int:
    rows = session.scalars(
        select(HatRiskPolicyVersion.version).where(HatRiskPolicyVersion.user_id == user_id)
    ).all()
    return max(rows, default=0) + 1


def supersede_old_seed(
    session: Session, user_id: uuid.UUID, now: datetime
) -> HatRiskPolicyVersion | None:
    """PR 46 (D128, HAT-RISK-011): when the user's policy in force at `now` is the withdrawn PR 10
    seed (`OLD_SEED_POLICY`) and nothing is pending, write the §1 seed as the next version,
    `direction = initial` and effective at once (the owner's decision, not a requested change,
    so no cooling-off), mark the old one superseded and write both governance events. Every
    number in it is equal to or stricter than the old seed. Returns the new row, or None and
    writes nothing for any other policy, a pending version, no policy, or a second run."""
    versions = load_versions(session, user_id)
    in_force = effective_version(versions, now)
    if in_force is None or in_force.policy != parse_policy(OLD_SEED_POLICY):
        return None
    if any(as_utc(v.effective_from) > as_utc(now) for v in versions):
        return None  # the owner has a change queued: leave the policy to it
    old = session.get_one(HatRiskPolicyVersion, in_force.version_id)
    row = HatRiskPolicyVersion(
        user_id=user_id,
        version=next_version(session, user_id),
        policy=dict(SEED_POLICY),
        direction="initial",
        requested_at=now,
        effective_from=now,
    )
    session.add(row)
    old.superseded_at = as_utc(now)
    session.flush()
    record_policy_event(session, row, EVENT_REQUESTED, "engine", None, now)
    record_policy_event(session, row, EVENT_EFFECTIVE, "engine", None, now)
    return row


def ensure_hat_policies(bind: Engine) -> int:
    """Startup upkeep (`app.database.ensure_hat_database`): `supersede_old_seed` for every user
    with a policy, each in its own transaction. Safe to repeat and to race: a second process
    that loses the version number to the first rolls back and leaves the first's row; any other
    constraint failure, after which the old seed is still in force (even with a change now
    pending), is raised. Returns
    how many users were moved to the new seed."""
    with Session(bind) as session:
        users = session.scalars(select(HatRiskPolicyVersion.user_id).distinct()).all()
    moved = 0
    for user_id in users:
        with Session(bind) as session:
            try:
                if supersede_old_seed(session, user_id, utc_now()) is not None:
                    session.commit()
                    moved += 1
            except IntegrityError:
                session.rollback()
                with Session(bind) as fresh:
                    in_force = effective_version(load_versions(fresh, user_id), utc_now())
                    if in_force is not None and in_force.policy == parse_policy(OLD_SEED_POLICY):
                        raise  # not a lost race: the old seed is still in force
    return moved
