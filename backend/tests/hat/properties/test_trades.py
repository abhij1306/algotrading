"""PBT trades.* (HAT-EXEC-045, HAT-BOT-050, HAT-BOT-083, HAT-EXEC-041): the entry path of
`app.hat.engine.trades` on SQLite, with the real risk checks."""

from __future__ import annotations

import uuid

from hat_engine_support import TRADE_DAY, open_trade, seed_engine_world
from hat_risk_support import policy_with, version_of
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.engine import lifecycle, trades
from app.hat.models import HatPosition, HatSignal, HatWatchlistEntry
from app.hat.risk.exposure import max_positions

PLAYBOOKS = ("stage2", "momentum", "episodic", "other")


@settings(max_examples=30)
@given(st.lists(st.sampled_from(PLAYBOOKS), min_size=1, max_size=9))
def test_shared_bot_cap(playbooks: list[str]) -> None:
    """HAT-BOT-050, HAT-EXEC-045: one bot's playbooks share the bot's position cap. However the
    triggers are spread over its playbooks, the accepted entries never exceed the cap, and a
    trigger is rejected for the cap exactly when the cap is full. Harm: each playbook gets its
    own cap and the bot holds a multiple of what the policy allows."""
    policy = policy_with(max_entries_per_day=20)  # leave the position cap as the binding one
    cap = max_positions("L2", policy)
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, instruments=len(playbooks))
        accepted = 0
        for playbook, instrument in zip(playbooks, world.instruments, strict=True):
            outcome = open_trade(
                session, world, instrument, playbook, policy_versions=[version_of(policy)]
            )
            if outcome.outcome.position_id is not None:
                accepted += 1
            else:
                signal = session.get(HatSignal, outcome.outcome.signal_id)
                assert signal.state == lifecycle.REJECTED
                assert accepted == cap  # a rejection happens only when the cap is full
        holding = session.scalar(
            select(func.count()).select_from(HatPosition).where(HatPosition.closed_at.is_(None))
        )
        assert holding == accepted == min(len(playbooks), cap)


@settings(max_examples=20)
@given(st.lists(st.booleans(), min_size=1, max_size=6))
def test_watchlist_expires_at_session_end(triggered: list[bool]) -> None:
    """HAT-EXEC-041: at the session end every watchlist row that did not trigger ends Expired and
    one that triggered does not; the count is the number expired; a second run changes nothing."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, instruments=len(triggered))
        for flag, instrument in zip(triggered, world.instruments, strict=True):
            session.add(
                HatWatchlistEntry(
                    id=uuid.uuid4(),
                    bot_id=world.bot_id,
                    strategy_version_id=world.version_id,
                    playbook="pb",
                    instrument_id=instrument,
                    trade_date=TRADE_DAY,
                    score=0.5,
                    criteria_results={},
                    status=lifecycle.WATCHLISTED,
                )
            )
            if flag:
                open_trade(session, world, instrument, "pb")
        session.flush()
        expired = trades.expire_watchlist(session, TRADE_DAY)
        assert expired == triggered.count(False)
        rows = session.scalars(select(HatWatchlistEntry)).all()
        assert sum(r.status == lifecycle.EXPIRED for r in rows) == expired
        assert sum(r.status == lifecycle.TRIGGERED for r in rows) == triggered.count(True)
        assert trades.expire_watchlist(session, TRADE_DAY) == 0
