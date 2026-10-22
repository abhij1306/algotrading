"""PBT policy.* (HAT-RISK-011, D128): the startup upkeep that moves a user still on the
withdrawn PR 10 seed to the 06 §1 seed, on SQLite through `ensure_hat_policies`."""

import uuid
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.hat.models import HatEvent, HatRiskPolicyVersion
from app.hat.risk import policy_upkeep
from app.hat.risk.policy import (
    OLD_SEED_POLICY,
    SEED_POLICY,
    classify_direction,
    effective_version,
    parse_policy,
    seed_policy,
)
from app.hat.risk.policy_upkeep import ensure_hat_policies, load_versions
from app.hat.time import as_utc, utc_now

# What the user's stored policy is before startup. `old` and `old_unmarked` are the PR 10 seed
# (the second written before PR 45 added `weights_confirmed`); the rest must be left alone.
STATES = ("none", "old", "old_unmarked", "new", "owner_changed", "old_with_pending")
MOVED = ("old", "old_unmarked")


def _document(state: str) -> dict[str, Any]:
    if state == "old_unmarked":
        return {k: v for k, v in OLD_SEED_POLICY.items() if k != "weights_confirmed"}
    if state == "new":
        return dict(SEED_POLICY)
    if state == "owner_changed":
        return {**OLD_SEED_POLICY, "risk_pct": "0.0020"}  # the owner's own decrease
    return dict(OLD_SEED_POLICY)


def _seed(session: Session, user: uuid.UUID, state: str) -> None:
    if state == "none":
        return
    past = utc_now() - timedelta(days=30)
    session.add(
        HatRiskPolicyVersion(
            user_id=user,
            version=1,
            policy=_document(state),
            direction="initial",
            requested_at=past,
            effective_from=past,
        )
    )
    if state == "old_with_pending":
        session.add(
            HatRiskPolicyVersion(
                user_id=user,
                version=2,
                policy={**OLD_SEED_POLICY, "risk_pct": "0.0020"},
                direction="decrease",
                requested_at=past,
                effective_from=utc_now() + timedelta(days=2),
            )
        )


def _rows(session: Session) -> list[tuple]:
    return sorted(
        (r.user_id, r.version, repr(r.policy), r.direction, r.superseded_at)
        for r in session.scalars(select(HatRiskPolicyVersion))
    )


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(STATES), min_size=1, max_size=3), st.integers(1, 3))
@example(states=["old"], runs=2)
@example(states=["old_unmarked", "owner_changed"], runs=1)
@example(states=["old_with_pending"], runs=1)
def test_seed_supersedes_old_initial(states: list[str], runs: int) -> None:
    """HAT-RISK-011 (PR 46, D128): for each user whose policy in force is the PR 10 seed, with
    `weights_confirmed` false or absent and nothing pending, startup writes the §1 seed as the
    next version, `initial` and effective at once, supersedes the old one at that instant and
    records `risk.change_requested` and `risk.change_effective`; the new seed is nowhere more
    permissive than the old. Any other policy, a pending change, no policy and every later run
    are left exactly as they were. Harm: an account keeps the 15% kill and 3% cap the owner
    withdrew and its bots stay in shadow, or the owner's own policy is overwritten."""
    assert classify_direction(parse_policy(OLD_SEED_POLICY), seed_policy()) == "decrease"
    engine = memory_engine()
    users = [uuid.uuid4() for _ in states]
    with Session(engine) as session:
        for user, state in zip(users, states, strict=True):
            _seed(session, user, state)
        session.commit()
        before = _rows(session)
    counts = [ensure_hat_policies(engine) for _ in range(runs)]
    expected = sum(state in MOVED for state in states)
    assert counts == [expected] + [0] * (runs - 1)
    with Session(engine) as session:
        for user, state in zip(users, states, strict=True):
            rows = sorted(
                session.scalars(
                    select(HatRiskPolicyVersion).where(HatRiskPolicyVersion.user_id == user)
                ),
                key=lambda r: r.version,
            )
            events = session.scalars(select(HatEvent).where(HatEvent.user_id == user)).all()
            if state not in MOVED:
                assert events == []
                continue
            old, new = rows
            assert (new.version, new.direction, new.policy) == (2, "initial", SEED_POLICY)
            assert as_utc(new.requested_at) == as_utc(new.effective_from)
            assert as_utc(old.superseded_at) == as_utc(new.effective_from)
            assert new.superseded_at is None
            assert parse_policy(new.policy).weights_confirmed is True
            assert sorted((e.event_type, e.actor, e.aggregate_id) for e in events) == [
                ("risk.change_effective", "engine", new.id),
                ("risk.change_requested", "engine", new.id),
            ]
        untouched = [r for r in before if states[users.index(r[0])] not in MOVED]
        assert [r for r in _rows(session) if states[users.index(r[0])] not in MOVED] == untouched


@settings(max_examples=20, deadline=None)
@given(st.lists(st.sampled_from(("none", "race", "other")), min_size=1, max_size=3))
@example(faults=["other"])
@example(faults=["race", "none"])
def test_upkeep_raises_unless_race_lost(faults: list[str]) -> None:
    """HAT-RISK-011 (PR 46): a constraint failure while moving a user off the old seed is ignored
    only when another process moved that user first (the version number race); any other one,
    after which the old seed would still be in force, is raised and startup fails. Harm: a
    swallowed failure leaves the withdrawn 15% kill and 3% cap in force while HAT runs."""
    engine = memory_engine()
    users = [uuid.uuid4() for _ in faults]
    with Session(engine) as session:
        for user in users:
            _seed(session, user, "old")
        session.commit()
    fault_of = dict(zip(users, faults, strict=True))
    real = policy_upkeep.supersede_old_seed
    seen: set[uuid.UUID] = set()

    def faulty(session: Session, user_id: uuid.UUID, now: datetime) -> HatRiskPolicyVersion | None:
        fault = fault_of[user_id] if user_id not in seen else "none"
        seen.add(user_id)
        if fault == "race":  # another process moves the user, then this flush collides
            with Session(engine) as other:
                real(other, user_id, now)
                other.commit()
        if fault != "none":
            raise IntegrityError("insert", {}, Exception("unique violation"))
        return real(session, user_id, now)

    with patch.object(policy_upkeep, "supersede_old_seed", faulty):
        if "other" in faults:
            with pytest.raises(IntegrityError):
                ensure_hat_policies(engine)
            return
        moved = ensure_hat_policies(engine)
    assert moved == faults.count("none")
    with Session(engine) as session:
        for user in users:
            in_force = effective_version(load_versions(session, user), utc_now())
            assert in_force is not None
            assert in_force.policy == seed_policy()
