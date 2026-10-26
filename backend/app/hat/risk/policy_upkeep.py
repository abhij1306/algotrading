"""Risk-policy reads and startup upkeep that need no backtest or order code (HAT-RISK-011).

The policy versions are config records (`configs.records.RiskPolicyVersion`, JSON files).
`app.database` runs `ensure_hat_policies` at startup, and the Telegram text path imports
`app.database`, so this module imports only the configs, the event log and `risk.policy`
(CONF `telegram.no_order_import_in_text_path`). `policy_service` builds on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..configs.records import RiskPolicyVersion
from ..configs.store import ConfigConflictError
from ..configs.unit import configs_of
from ..events import record
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
    rows = configs_of(session).all(RiskPolicyVersion, lambda r: r.user_id == user_id)
    return [
        PolicyVersion(r.id, r.version, r.effective_from, r.superseded_at, parse_policy(r.policy))
        for r in rows
    ]


EVENT_REQUESTED = "risk.change_requested"
EVENT_EFFECTIVE = "risk.change_effective"
EVENT_WITHDRAWN = "risk.change_withdrawn"  # a pending version a decrease withdrew


def record_policy_event(
    session: Session,
    row: RiskPolicyVersion,
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
    rows = configs_of(session).all(RiskPolicyVersion, lambda r: r.user_id == user_id)
    return max((r.version for r in rows), default=0) + 1


def supersede_old_seed(
    session: Session, user_id: uuid.UUID, now: datetime
) -> RiskPolicyVersion | None:
    """PR 46 (D128, HAT-RISK-011): when the user's policy in force at `now` is the withdrawn PR 10
    seed (`OLD_SEED_POLICY`) and nothing is pending, write the current seed as the next version,
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
    old = configs_of(session).get(RiskPolicyVersion, in_force.version_id)
    assert old is not None  # a loaded version
    row = RiskPolicyVersion(
        user_id=user_id,
        version=next_version(session, user_id),
        policy=dict(SEED_POLICY),
        direction="initial",
        requested_at=now,
        effective_from=now,
    )
    configs_of(session).add(row)
    old.superseded_at = as_utc(now)
    record_policy_event(session, row, EVENT_REQUESTED, "engine", None, now)
    record_policy_event(session, row, EVENT_EFFECTIVE, "engine", None, now)
    return row


def ensure_hat_policies(session_factory: Callable[[], Session]) -> int:
    """Startup upkeep (`app.database.ensure_hat_database`): `supersede_old_seed` for every user
    with a policy, each in its own transaction. Safe to repeat and to race: a second process
    that loses the version files to the first (`ConfigConflictError`) rolls back and leaves the
    first's; when the old seed is still in force after that, the conflict is raised. Returns
    how many users were moved to the new seed."""
    with session_factory() as session:
        users = {r.user_id for r in configs_of(session).all(RiskPolicyVersion)}
    moved = 0
    for user_id in sorted(users):
        with session_factory() as session:
            try:
                if supersede_old_seed(session, user_id, utc_now()) is not None:
                    session.commit()
                    moved += 1
            except ConfigConflictError:
                session.rollback()
                with session_factory() as fresh:
                    in_force = effective_version(load_versions(fresh, user_id), utc_now())
                    if in_force is not None and in_force.policy == parse_policy(OLD_SEED_POLICY):
                        raise  # not a lost race: the old seed is still in force
    return moved
