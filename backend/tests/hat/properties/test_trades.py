"""PBT trades.* (HAT-EXEC-045, HAT-BOT-050, HAT-BOT-083, HAT-EXEC-041, HAT-RISK-030): the entry
path of `app.hat.engine.trades` on SQLite, with the real risk checks."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from hat_engine_support import TRADE_DAY, add_bot, open_trade, seed_engine_world
from hat_risk_support import decide, policy_with, valid_ctx, version_of
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.engine import lifecycle, trades
from app.hat.models import HatBot, HatPosition, HatSignal, HatUniverseMember, HatWatchlistEntry
from app.hat.risk.decision import Reason
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
        stored = replace(version_of(policy), version_id=world.policy_version_id)
        accepted = 0
        for playbook, instrument in zip(playbooks, world.instruments, strict=True):
            outcome = open_trade(session, world, instrument, playbook, policy_versions=[stored])
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


@st.composite
def _two_bot_triggers(draw: st.DrawFn) -> list[int]:
    """An interleaving of triggers of bot 0 and bot 1, each within its own position cap."""
    cap = max_positions("L2", policy_with())
    first, second = draw(st.integers(0, cap)), draw(st.integers(0, cap))
    return draw(st.permutations([0] * first + [1] * second))


def _owner_open_risk(session: Session) -> Decimal:
    """(entry - stop) x qty over every open position, whatever its bot."""
    return sum(
        (
            (Decimal(p.avg_entry) - Decimal(p.current_stop)) * p.qty_open
            for p in session.scalars(select(HatPosition).where(HatPosition.closed_at.is_(None)))
        ),
        Decimal(0),
    )


@settings(max_examples=40)
@given(_two_bot_triggers(), st.integers(1, 7), st.integers(1, 2499))
def test_global_open_risk_across_bots(order: list[int], room: int, spare: int) -> None:
    """HAT-RISK-030 through `open_entry`: two bots of one owner, on different broker accounts,
    share the 3% global open-risk cap. However their triggers interleave, a trigger is rejected
    with `global_open_risk` exactly when the open risk of both bots plus its own exceeds 3% of
    total equity, and is accepted otherwise; the owner never holds more. Harm: each bot reads
    only its own positions, so N bots hold N x 3%."""
    policy = policy_with(max_entries_per_day=20)  # the per-bot caps never bind here
    risk = decide(valid_ctx(total_equity=Decimal(10**12))).risk_amount
    total_equity = (risk * room + spare) / Decimal("0.03")  # room for `room` entries, not more
    cap = Decimal("0.03") * total_equity
    engine = memory_engine()
    with Session(engine) as session:
        first = seed_engine_world(session, instruments=len(order) or 1)
        stored = replace(version_of(policy), version_id=first.policy_version_id)
        worlds = (first, add_bot(session, first))
        for n, which in enumerate(order):
            held = _owner_open_risk(session)
            outcome = open_trade(
                session,
                worlds[which],
                first.instruments[n],
                policy_versions=[stored],
                total_equity=total_equity,
            ).outcome
            if held + risk > cap:
                assert outcome.position_id is None
                assert outcome.decision.reason == Reason.GLOBAL_OPEN_RISK
            else:
                assert outcome.position_id is not None, outcome.decision
            assert _owner_open_risk(session) <= cap
        holding = session.scalar(
            select(func.count()).select_from(HatPosition).where(HatPosition.closed_at.is_(None))
        )
        assert holding == min(len(order), room)


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


@settings(max_examples=20)
@given(st.booleans(), st.booleans())
def test_retry_returns_existing_signal(mode_changed: bool, left_universe: bool) -> None:
    """HAT-EXEC-021: a repeated trigger for the same (bot, playbook, instrument, day) returns the
    signal it already made and writes nothing, even after the bot left shadow mode or the
    instrument left the universe in between. Harm: a job retried after such a change crashes
    instead of finding the trade it already took, and its caller cannot tell it was taken."""
    with Session(memory_engine()) as session:
        world = seed_engine_world(session, instruments=1)
        instrument = world.instruments[0]
        first = open_trade(session, world, instrument).outcome
        if mode_changed:
            session.get(HatBot, world.bot_id).execution_mode = "confirm"
        if left_universe:
            for member in session.scalars(select(HatUniverseMember)):
                member.effective_to = TRADE_DAY
        session.flush()
        again = open_trade(session, world, instrument).outcome
        assert again.duplicate
        assert again.signal_id == first.signal_id
        assert session.scalar(select(func.count()).select_from(HatSignal)) == 1
