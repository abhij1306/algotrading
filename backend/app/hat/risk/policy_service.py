"""Risk-policy changes as database operations (HAT-GOV-090..094).

A change is requested as a new risk-policy version (a config record, `configs`): a decrease takes
effect at the next session open, an increase no sooner than 7 days after the request and only
with gate evidence, which by default comes from the backtest runs
(`backtest.evidence.gate_evidence`, HAT-GOV-091).
Requesting writes `risk.change_requested`; a version taking effect writes `risk.change_effective`
and marks the version it replaces `superseded_at`, in the same transaction. A decrease withdraws
every version still pending (`risk.change_withdrawn`, `superseded_at` before its own
`effective_from`), so a later decrease is never overridden by an earlier increase. Before
anything has traded for real, the owner may instead replace the policy at once
(`reset_policy_before_trading`). Nothing commits here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session

from ..backtest.evidence import gate_evidence
from ..configs.records import RiskPolicyVersion
from ..configs.unit import configs_of
from ..errors import HatError
from ..governance import COOLING_OFF
from ..models import HatEvent, HatOrder, HatPosition, HatTradeResult
from ..time import as_utc
from .policy import (
    SEED_POLICY,
    RiskPolicy,
    classify_direction,
    effective_version,
    pre_trading,
    require_increase_gate,
    withdrawn,
    withdrawn_by_decrease,
)
from .policy_upkeep import (
    EVENT_EFFECTIVE,
    EVENT_REQUESTED,
    EVENT_WITHDRAWN,
    next_version,
    record_policy_event,
)
from .policy_upkeep import load_versions as load_versions  # callers import it from here
from .sessions import SessionCalendar, SessionState

# The evidence an increase is checked with, for a user: None when there is none.
EvidenceSource = Callable[[Session, uuid.UUID, datetime], Mapping[str, Any] | None]


def backtest_evidence(
    session: Session, user_id: uuid.UUID, at: datetime
) -> Mapping[str, Any] | None:
    """The user's trades closed since the policy version in force at `at` took effect, against
    the discounted expectancy of the sleeves' newest completed backtest runs; None (the increase
    is refused) when no sleeve has one."""
    return gate_evidence(session, user_id, at)


class PolicyChangeError(HatError):
    """A policy change was refused (no known session open to take effect at)."""


def seed_initial_policy(session: Session, user_id: uuid.UUID, now: datetime) -> RiskPolicyVersion:
    """Version 1 (`direction = initial`), effective now, with seed; a no-op that returns
    the first version when one exists. Writes both governance events."""
    found = configs_of(session).all(
        RiskPolicyVersion, lambda r: r.user_id == user_id and r.version == 1
    )
    if found:
        return found[0]
    row = RiskPolicyVersion(
        user_id=user_id,
        version=1,
        policy=dict(SEED_POLICY),
        direction="initial",
        requested_at=now,
        effective_from=now,
    )
    configs_of(session).add(row)
    record_policy_event(session, row, EVENT_REQUESTED, "engine", None, now)
    record_policy_event(session, row, EVENT_EFFECTIVE, "engine", None, now)
    return row


class PolicyResetError(HatError):
    """A pre-trading policy reset was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _any(session: Session, query: Select[Any]) -> int:
    return 1 if session.scalar(select(exists(query))) else 0


def reset_policy_before_trading(
    session: Session,
    *,
    user_id: uuid.UUID,
    policy: RiskPolicy,
    calendar: SessionCalendar,
    chat_id: int,
    now: datetime,
) -> RiskPolicyVersion:
    """Replace the policy in force with `policy` at once, as the next version (`direction =
    initial`, effective now, like the seed): the in-force version is superseded and every pending
    one withdrawn (`risk.change_withdrawn`), and the owner's `risk.change_requested` and
    `risk.change_effective` are written. Only while nothing has traded for real (`pre_trading`:
    no real order, position or closed trade anywhere in the database); then a change has no
    live record to protect, so no cooling-off or evidence gate applies. Refused, writing nothing
    (`PolicyResetError`): inside market hours (`policy_reset_in_session`), once anything traded
    for real (`policy_reset_after_trading`), and with no policy yet (`no_policy`)."""
    if calendar.state(now) is not SessionState.CLOSED:
        raise PolicyResetError("policy_reset_in_session")
    real_trades = (
        select(HatTradeResult.position_id)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .where(HatPosition.is_virtual.is_(False))
    )
    if not pre_trading(
        _any(session, select(HatOrder.id).where(HatOrder.is_virtual.is_(False))),
        _any(session, select(HatPosition.id).where(HatPosition.is_virtual.is_(False))),
        _any(session, real_trades),
    ):
        raise PolicyResetError("policy_reset_after_trading")
    versions = load_versions(session, user_id)
    if effective_version(versions, now) is None:
        raise PolicyResetError("no_policy")
    row = RiskPolicyVersion(
        user_id=user_id,
        version=next_version(session, user_id),
        policy=policy.to_json(),
        direction="initial",
        requested_at=now,
        effective_from=now,
    )
    configs_of(session).add(row)
    record_policy_event(session, row, EVENT_REQUESTED, "user", chat_id, now)
    replaced = {v.version_id for v in versions if v.superseded_at is None}
    for old in configs_of(session).all(RiskPolicyVersion, lambda r: r.id in replaced):
        old.superseded_at = as_utc(now)
        if as_utc(old.effective_from) > as_utc(now):  # pending: withdrawn, never in force
            record_policy_event(session, old, EVENT_WITHDRAWN, "user", chat_id, now)
    record_policy_event(session, row, EVENT_EFFECTIVE, "user", chat_id, now)
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
) -> RiskPolicyVersion:
    """Queue `proposed` as the next version (`risk.change_requested`), outside market hours only.

    The direction is `increase` when `proposed` is more permissive than the version in force or
    than any pending one, else `decrease`. A decrease takes effect at the next session open and
    withdraws every pending version (`withdrawn_by_decrease`, `risk.change_withdrawn`); an
    increase at the first session open at least 7 days after the request, and `GateError` is
    raised unless the evidence `evidence` reads (by default the backtest runs) passes the gate.
    Raises `PolicyChangeError` for the owner's request (`actor = user`) without the allowlisted
    chat id its event carries (PR 73; PR 46 follow-up 4), inside market hours (or when the
    calendar does not know the day) and when the calendar holds no such open; each before
    anything is written.
    """
    if actor == "user" and chat_id is None:
        raise PolicyChangeError("the owner's policy change needs the chat id")
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
    gate_evidence = evidence(session, user_id, requested_at) if direction == "increase" else None
    require_increase_gate(direction, gate_evidence)
    if direction == "increase":
        effective = calendar.first_open_at_or_after(as_utc(requested_at) + COOLING_OFF)
    else:
        effective = calendar.next_open_after(requested_at)
    if effective is None:
        raise PolicyChangeError("the calendar has no session open to take effect at")
    row = RiskPolicyVersion(
        user_id=user_id,
        version=next_version(session, user_id),
        policy=proposed.to_json(),
        direction=direction,
        requested_at=requested_at,
        effective_from=effective,
        gate_evidence=dict(gate_evidence) if gate_evidence is not None else None,
    )
    configs_of(session).add(row)
    record_policy_event(session, row, EVENT_REQUESTED, actor, chat_id, requested_at)
    if direction == "decrease":
        ids = {v.version_id for v in withdrawn_by_decrease(versions, requested_at)}
        for old in configs_of(session).all(RiskPolicyVersion, lambda r: r.id in ids):
            old.superseded_at = as_utc(requested_at)
            record_policy_event(session, old, EVENT_WITHDRAWN, actor, chat_id, requested_at)
    return row


def apply_due_versions(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Make every version whose time has come take effect: write `risk.change_effective` once
    for it and mark the version it replaces `superseded_at`. Safe to repeat. Returns how many
    versions took effect on this call."""
    rows = configs_of(session).all(RiskPolicyVersion, lambda r: r.user_id == user_id)
    announced = set(
        session.scalars(
            select(HatEvent.aggregate_id).where(
                HatEvent.event_type == EVENT_EFFECTIVE,
                HatEvent.aggregate_type == "risk_policy",
            )
        ).all()
    )
    due = sorted(
        (
            r
            for r in rows
            if as_utc(r.effective_from) <= as_utc(now)
            and r.id not in announced
            and not withdrawn(r.effective_from, r.superseded_at)
        ),
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
        record_policy_event(
            session, row, EVENT_EFFECTIVE, "scheduler", None, as_utc(row.effective_from)
        )
    return len(due)
