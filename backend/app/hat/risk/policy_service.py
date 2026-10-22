"""Risk-policy changes as database operations (06 §9, HAT-GOV-090..094).

A change is requested as a new `hat_risk_policy_version`: a decrease takes effect at the next
session open, an increase no sooner than 7 days after the request and only with gate evidence,
which by default comes from the backtest runs (`backtest.runs.gate_evidence`, HAT-GOV-091).
Requesting writes `risk.change_requested`; a version taking effect writes `risk.change_effective`
and marks the version it replaces `superseded_at`, in the same transaction. Nothing commits here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..events import record
from ..models import HatEvent, HatRiskPolicyVersion
from ..time import as_utc
from .policy import (
    COOLING_OFF,
    SEED_POLICY,
    PolicyVersion,
    RiskPolicy,
    classify_direction,
    parse_policy,
    require_increase_gate,
)
from .sessions import SessionCalendar, SessionState

# The evidence an increase is checked with, for a user: None when there is none.
EvidenceSource = Callable[[Session, uuid.UUID], Mapping[str, Any] | None]


def backtest_evidence(session: Session, user_id: uuid.UUID) -> Mapping[str, Any] | None:
    """The user's closed trades against the discounted expectancy of the sleeves' newest completed
    backtest runs; None (the increase is refused) when no sleeve has one."""
    # Imported here: `backtest.runs` reads `load_versions` from this module.
    from ..backtest.runs import gate_evidence

    return gate_evidence(session, user_id)


class PolicyChangeError(HatError):
    """A policy change was refused (no known session open to take effect at)."""


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


def _event(
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


def _next_version(session: Session, user_id: uuid.UUID) -> int:
    rows = session.scalars(
        select(HatRiskPolicyVersion.version).where(HatRiskPolicyVersion.user_id == user_id)
    ).all()
    return max(rows, default=0) + 1


def seed_initial_policy(
    session: Session, user_id: uuid.UUID, now: datetime
) -> HatRiskPolicyVersion:
    """Version 1 (`direction = initial`), effective now, with the 06 §1 seed; a no-op that returns
    the first version when one exists. Writes both governance events."""
    first = session.scalars(
        select(HatRiskPolicyVersion).where(
            HatRiskPolicyVersion.user_id == user_id, HatRiskPolicyVersion.version == 1
        )
    ).first()
    if first is not None:
        return first
    row = HatRiskPolicyVersion(
        user_id=user_id,
        version=1,
        policy=dict(SEED_POLICY),
        direction="initial",
        requested_at=now,
        effective_from=now,
    )
    session.add(row)
    session.flush()
    _event(session, row, EVENT_REQUESTED, "engine", None, now)
    _event(session, row, EVENT_EFFECTIVE, "engine", None, now)
    return row


def request_policy_change(
    session: Session,
    *,
    user_id: uuid.UUID,
    proposed: RiskPolicy,
    calendar: SessionCalendar,
    requested_at: datetime,
    evidence: EvidenceSource = backtest_evidence,
    actor: str = "user",
    chat_id: int | None = None,
) -> HatRiskPolicyVersion:
    """Queue `proposed` as the next version (`risk.change_requested`), outside market hours only.

    The direction is `increase` when `proposed` is more permissive than the version in force or
    than any pending one, else `decrease`. A decrease takes effect at the next session open; an
    increase at the first session open at least 7 days after the request, and `GateError` is
    raised unless the evidence `evidence` reads (by default the backtest runs) passes the gate.
    Raises `PolicyChangeError` inside market hours (or when the calendar does not know the day)
    and when the calendar holds no such open.
    """
    if calendar.state(requested_at) is not SessionState.CLOSED:
        raise PolicyChangeError("a policy change is requested outside market hours")
    versions = load_versions(session, user_id)
    in_force = [
        v
        for v in versions
        if v.superseded_at is None and as_utc(v.effective_from) <= as_utc(requested_at)
    ]
    pending = [v for v in versions if as_utc(v.effective_from) > as_utc(requested_at)]
    reference = (
        [max(in_force, key=lambda v: (as_utc(v.effective_from), v.version)), *pending]
        if in_force
        else pending
    )
    if not reference:
        raise PolicyChangeError("there is no policy to change; seed the initial version first")
    direction = (
        "increase"
        if any(classify_direction(v.policy, proposed) == "increase" for v in reference)
        else "decrease"
    )
    gate_evidence = evidence(session, user_id) if direction == "increase" else None
    require_increase_gate(direction, gate_evidence)
    if direction == "increase":
        effective = calendar.first_open_at_or_after(as_utc(requested_at) + COOLING_OFF)
    else:
        effective = calendar.next_open_after(requested_at)
    if effective is None:
        raise PolicyChangeError("the calendar has no session open to take effect at")
    row = HatRiskPolicyVersion(
        user_id=user_id,
        version=_next_version(session, user_id),
        policy=proposed.to_json(),
        direction=direction,
        requested_at=requested_at,
        effective_from=effective,
        gate_evidence=dict(gate_evidence) if gate_evidence is not None else None,
    )
    session.add(row)
    session.flush()
    _event(session, row, EVENT_REQUESTED, actor, chat_id, requested_at)
    return row


def apply_due_versions(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Make every version whose time has come take effect: write `risk.change_effective` once
    for it and mark the version it replaces `superseded_at`. Safe to repeat. Returns how many
    versions took effect on this call."""
    rows = session.scalars(
        select(HatRiskPolicyVersion).where(HatRiskPolicyVersion.user_id == user_id)
    ).all()
    announced = set(
        session.scalars(
            select(HatEvent.aggregate_id).where(
                HatEvent.event_type == EVENT_EFFECTIVE,
                HatEvent.aggregate_type == "risk_policy",
            )
        ).all()
    )
    due = sorted(
        (r for r in rows if as_utc(r.effective_from) <= as_utc(now) and r.id not in announced),
        key=lambda r: (as_utc(r.effective_from), r.version),
    )
    for row in due:
        for older in rows:
            if (
                older.id != row.id
                and older.superseded_at is None
                and (as_utc(older.effective_from), older.version)
                < (as_utc(row.effective_from), row.version)
            ):
                older.superseded_at = as_utc(row.effective_from)
        _event(session, row, EVENT_EFFECTIVE, "scheduler", None, as_utc(row.effective_from))
    return len(due)
