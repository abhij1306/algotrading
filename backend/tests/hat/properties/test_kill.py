"""PBT kill.* (HAT-SCH-033/034, HAT-RISK-062..065, HAT-DEC-B23..B26): kill-switch state from rows
and through the real service on SQLite, against an independently written model."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from hat_risk_support import AT, D, seed_bot, seed_pending_approval
from hat_support import memory_engine
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.hat.models import HatApproval, HatDailyBotSnapshot, HatEvent, HatKillSwitch, HatSignal
from app.hat.risk import kill
from app.hat.risk.controls import (
    ControlError,
    evaluate_drawdown_kills,
    kill_state,
    pause_bot,
    rearm_kill,
    resume_bot,
    snapshot_bots,
    sync_health,
    trip_kill,
)
from app.hat.risk.kill import ARMED, TRIPPED, KillRow
from app.hat.risk.policy import seed_policy

BOT_A, BOT_B = uuid.uuid4(), uuid.uuid4()
REASONS = kill.REASONS


@st.composite
def kill_rows(draw: st.DrawFn) -> list[KillRow]:
    rows = []
    for _ in range(draw(st.integers(0, 10))):
        scope = draw(st.sampled_from(("account", "bot")))
        state = draw(st.sampled_from((TRIPPED, ARMED)))
        at = AT + timedelta(minutes=draw(st.integers(0, 6)))
        rows.append(
            KillRow(
                scope,
                BOT_A if scope == "bot" else None,
                state,
                at,
                draw(st.sampled_from(REASONS)),
                at if state == ARMED else None,
            )
        )
    return rows


@given(
    kill_rows(),
    st.sampled_from(("account", "bot")),
    st.one_of(st.none(), st.sampled_from(("risk", "health"))),
)
def test_current_state(rows: list, scope: str, channel: str | None) -> None:
    """A (scope, bot) is tripped exactly when some stream's latest row is a trip: the risk
    reasons (drawdown, manual) form one stream and each health reason (data, feed, broker) its
    own, so an automatic health re-arm never hides a tripped drawdown or manual kill nor another
    source's health trip; with no rows it is armed; on a time tie the trip wins."""
    bot = BOT_A if scope == "bot" else None
    mine = [r for r in rows if r.scope == scope]
    if channel is not None:
        mine = [r for r in mine if kill.channel_of(r.reason) == channel]
    expected = ARMED
    for name in ("risk", *kill.HEALTH_REASONS):
        group = [
            r for r in mine if (r.reason if r.reason in kill.HEALTH_REASONS else "risk") == name
        ]
        if not group:
            continue
        top = max(r.occurred_at for r in group)
        if any(r.state == TRIPPED for r in group if r.occurred_at == top):
            expected = TRIPPED
    assert kill.current_state(rows, scope, bot, channel) == expected


@given(kill_rows())
def test_current_state_other_key_untouched(rows: list) -> None:
    """Rows of one bot never change another bot's state, nor the account's."""
    assert kill.current_state(rows, "bot", BOT_B) == ARMED
    assert kill.kill_state_of(rows, BOT_B, paused=False).bot_tripped is False


# --- the service, as a state machine ---------------------------------------------------------

# The account's health sources an op's index picks: the quality gate and the live feed.
ACCOUNT_HEALTH = ("data_health", "feed_health")

OPS = st.lists(
    st.tuples(
        st.sampled_from(
            (
                "trip_bot",
                "trip_account",
                "health_fail",
                "health_ok",
                "account_health_fail",
                "account_health_ok",
                "rearm_bot",
                "rearm_account",
                "pause",
                "resume",
                "rearm_unconfirmed",
                "rearm_by_engine",
            )
        ),
        st.integers(0, 1),
    ),
    min_size=1,
    max_size=25,
)


@settings(max_examples=300)
@given(OPS)
def test_kill_service_matches_model(ops: list) -> None:
    """After any sequence of trips, health changes, re-arms, pauses and resumes: a bot is
    manage-only exactly when the account kill, its own kill, its health kill, the account
    health kill or its pause says so; a pause or resume never touches a kill; a health pass
    never re-arms a drawdown or manual kill; a health source (the gate's `data_health`, the
    feed's `feed_health`) clears only its own trip, so the feed recovering never clears a failed
    gate (HAT-DATA-065); a drawdown or manual re-arm needs a user and a
    second confirmation; every state change appends its event."""
    engine = memory_engine()
    with Session(engine) as session:
        bots = [seed_bot(session), seed_bot(session)]
        session.commit()
        risk = {"acct": False, "bot": [False, False]}
        health: dict = {"acct": dict.fromkeys(ACCOUNT_HEALTH, False), "bot": [False, False]}
        paused = [False, False]
        events = 0
        clock = AT
        for op, i in ops:
            clock += timedelta(minutes=1)
            bot = bots[i]
            if op == "trip_bot":
                row = trip_kill(session, scope="bot", bot_id=bot, reason="drawdown_bot", now=clock)
                assert (row is not None) is (not risk["bot"][i])
                events += row is not None
                risk["bot"][i] = True
            elif op == "trip_account":
                row = trip_kill(session, scope="account", bot_id=None, reason="manual", now=clock)
                assert (row is not None) is (not risk["acct"])
                events += row is not None
                risk["acct"] = True
            elif op in ("health_fail", "health_ok"):
                row = sync_health(
                    session,
                    scope="bot",
                    bot_id=bot,
                    failing=op == "health_fail",
                    reason="data_health",
                    now=clock,
                )
                changed = health["bot"][i] != (op == "health_fail")
                assert (row is not None) is changed
                events += row is not None
                health["bot"][i] = op == "health_fail"
            elif op in ("account_health_fail", "account_health_ok"):
                source = ACCOUNT_HEALTH[i]
                row = sync_health(
                    session,
                    scope="account",
                    bot_id=None,
                    failing=op == "account_health_fail",
                    reason=source,
                    now=clock,
                )
                changed = health["acct"][source] != (op == "account_health_fail")
                assert (row is not None) is changed
                events += row is not None
                health["acct"][source] = op == "account_health_fail"
            elif op in ("rearm_bot", "rearm_account"):
                scope = "bot" if op == "rearm_bot" else "account"
                row = rearm_kill(
                    session,
                    scope=scope,
                    bot_id=bot if scope == "bot" else None,
                    confirmed=True,
                    now=clock,
                    chat_id=7,
                )
                was = risk["bot"][i] if scope == "bot" else risk["acct"]
                assert (row is not None) is was
                events += row is not None
                if scope == "bot":
                    risk["bot"][i] = False
                else:
                    risk["acct"] = False
            elif op == "rearm_unconfirmed":
                with pytest.raises(ControlError):
                    rearm_kill(
                        session, scope="bot", bot_id=bot, confirmed=False, now=clock, chat_id=7
                    )
            elif op == "rearm_by_engine":
                with pytest.raises(ControlError):
                    rearm_kill(
                        session,
                        scope="bot",
                        bot_id=bot,
                        confirmed=True,
                        now=clock,
                        chat_id=7,
                        actor="engine",
                    )
            elif op == "pause":
                changed = pause_bot(session, bot, clock, chat_id=7)
                assert changed is (not paused[i])
                events += changed
                paused[i] = True
            else:
                changed = resume_bot(session, bot, clock, chat_id=7)
                assert changed is paused[i]
                events += changed
                paused[i] = False
            session.commit()
            for j, other in enumerate(bots):
                state = kill_state(session, other)
                assert state.account_tripped == risk["acct"]
                assert state.bot_tripped == risk["bot"][j]
                account_health = any(health["acct"].values())
                assert state.health_tripped == (health["bot"][j] or account_health)
                assert state.paused == paused[j]
                assert state.manage_only == (
                    risk["acct"]
                    or risk["bot"][j]
                    or health["bot"][j]
                    or account_health
                    or paused[j]
                )
        total = len(session.scalars(select(HatEvent)).all())
        assert total == events


@given(
    st.sampled_from(("account", "bot")),
    st.sampled_from(("manual", "drawdown_bot", "drawdown_account")),
)
def test_trip_cancels_pending_approvals(scope: str, reason: str) -> None:
    """A drawdown or manual trip ends the undecided approvals it covers (the bot's, or all for
    the account) as `expired` with `signal.expired`; decided ones and other bots' (for a bot kill)
    are left; a health trip cancels none."""
    engine = memory_engine()
    with Session(engine) as session:
        a, b = seed_bot(session), seed_bot(session)
        sa, sb = seed_pending_approval(session, a), seed_pending_approval(session, b)
        session.commit()
        trip_kill(session, scope=scope, bot_id=a if scope == "bot" else None, reason=reason, now=AT)
        session.commit()
        decisions = {r.signal_id: r.decision for r in session.scalars(select(HatApproval))}
        assert decisions[sa] == "expired"
        assert decisions[sb] == ("expired" if scope == "account" else None)
        expired = [e for e in session.scalars(select(HatEvent)) if e.event_type == "signal.expired"]
        assert len(expired) == (2 if scope == "account" else 1)
        # The state follows the event (HAT-EXEC-042): cancelled ones are Expired, others wait.
        states = {s.id: s.state for s in session.scalars(select(HatSignal))}
        assert states[sa] == "Expired"
        assert states[sb] == ("Expired" if scope == "account" else "AwaitingApproval")
    with Session(memory_engine()) as session:
        a = seed_bot(session)
        s = seed_pending_approval(session, a)
        trip_kill(session, scope="bot", bot_id=a, reason="data_health", now=AT)
        session.commit()
        assert session.get(HatApproval, s).decision is None


@given(
    st.lists(
        st.tuples(
            st.decimals(min_value=D("60"), max_value=D("140"), places=2),
            st.decimals(min_value=D("60"), max_value=D("140"), places=2),
        ),
        min_size=1,
        max_size=12,
    ),
    st.booleans(),
)
def test_drawdown_kills_from_snapshots(equities: list, rearm_first: bool) -> None:
    """From daily snapshots the account kill trips when the summed equity is 15% under its
    running peak and a bot kill when that bot is 10% under its own, nothing else trips, and a
    re-arm is not undone by the snapshots it already saw."""
    from datetime import date

    policy = seed_policy()
    engine = memory_engine()
    with Session(engine) as session:
        bots = [seed_bot(session), seed_bot(session)]
        peaks = [D(0), D(0)]
        account_peak = D(0)
        expect_bot = [False, False]
        expect_account = False
        for day, pair in enumerate(equities):
            d = date(2026, 3, 2) + timedelta(days=day)
            account = sum(pair, D(0))
            account_peak = max(account_peak, account)
            expect_account |= (account_peak - account) * 100 >= 15 * account_peak
            for k in range(2):
                peaks[k] = max(peaks[k], pair[k])
                expect_bot[k] |= (peaks[k] - pair[k]) * 100 >= 10 * peaks[k]
                session.add(
                    HatDailyBotSnapshot(
                        bot_id=bots[k],
                        trade_date=d,
                        equity=pair[k],
                        equity_peak=peaks[k],
                        open_risk=D(0),
                        realized_r=D(0),
                        exposure_level="L1",
                        currency="INR",
                    )
                )
        session.commit()
        now = AT + timedelta(days=400)
        tripped = evaluate_drawdown_kills(session, policy, now)
        session.commit()
        assert ("account" in tripped) is expect_account
        assert {str(b) for b in bots if str(b) in tripped} == {
            str(bots[k]) for k in range(2) if expect_bot[k]
        }
        if rearm_first and expect_account:
            rearm_kill(
                session,
                scope="account",
                bot_id=None,
                confirmed=True,
                now=now + timedelta(minutes=1),
                chat_id=1,
            )
            session.commit()
            assert evaluate_drawdown_kills(session, policy, now + timedelta(minutes=2)) == []
        session.execute(delete(HatKillSwitch))
        session.execute(delete(HatDailyBotSnapshot))
        session.commit()


# --- planned reallocation is not a drawdown (HAT-RISK-066) --------------------------------------

CAPITAL = st.integers(1, 10_000_000).map(lambda n: D(n) / 100)


@given(st.lists(st.tuples(CAPITAL, st.booleans()), min_size=2, max_size=12))
def test_reallocation_is_not_drawdown(days: list) -> None:
    """The allocator moves a sleeve's capital between nights (regime, health, cluster caps) and
    snapshot equity is allocation plus realised P&L. With no trade results, whatever sequence of
    capitals (a sleeve benched for a night included) the bot and account kills stay armed, the
    stored peak never exceeds the equity it is restated for, and the restated drawdown is 0."""
    from datetime import date

    from app.hat.models import HatAllocationDaily

    engine = memory_engine()
    with Session(engine) as session:
        bot = seed_bot(session)
        first = date(2026, 3, 2)
        for offset, (capital, benched) in enumerate(days):
            amount = D(0) if benched and offset else capital
            session.add(
                HatAllocationDaily(
                    trade_date=first + timedelta(days=offset),
                    bot_id=bot,
                    base_weight=D("0.5"),
                    regime_mult=D(1),
                    health_mult=D(1),
                    final_weight=D("0.5"),
                    capital=amount,
                    risk_pct=D("0.0025"),
                    currency="INR",
                )
            )
            session.flush()
            snapshot_bots(session, first + timedelta(days=offset))
        session.commit()
        for snap in session.scalars(select(HatDailyBotSnapshot)):
            assert snap.equity_peak <= snap.equity + D("0.0001") or snap.equity == 0
        assert evaluate_drawdown_kills(session, seed_policy(), AT + timedelta(days=400)) == []


@given(
    CAPITAL,
    st.integers(25, 100).map(lambda n: D(n) / 100),
    st.integers(0, 40).map(lambda n: D(n) / 100),
)
def test_reallocation_does_not_hide_a_loss(
    capital: Decimal, keep: Decimal, loss_share: Decimal
) -> None:
    """A real loss still trips after the capital was cut the same night: with the allocation cut
    to `keep` x capital and `loss_share` of the new capital lost, the bot kill trips exactly
    when that loss is at least the policy's 10% of the capital it was measured against."""
    from datetime import date, datetime, time

    from app.hat.data.sessions import IST
    from app.hat.models import HatAllocationDaily, HatPosition, HatTradeResult

    new_capital = (capital * keep).quantize(D("0.01"))
    assume(new_capital > 0)
    loss = (new_capital * loss_share).quantize(D("0.01"))
    engine = memory_engine()
    with Session(engine) as session:
        bot = seed_bot(session)
        first, second = date(2026, 3, 2), date(2026, 3, 3)
        for day, amount in ((first, capital), (second, new_capital)):
            session.add(
                HatAllocationDaily(
                    trade_date=day,
                    bot_id=bot,
                    base_weight=D("0.5"),
                    regime_mult=D(1),
                    health_mult=D(1),
                    final_weight=D("0.5"),
                    capital=amount,
                    risk_pct=D("0.0025"),
                    currency="INR",
                )
            )
        position = HatPosition(
            id=uuid.uuid4(),
            bot_id=bot,
            signal_id=uuid.uuid4(),
            instrument_id=uuid.uuid4(),
            is_virtual=False,
            qty_open=0,
            avg_entry=D(100),
            initial_stop=D(95),
            current_stop=D(95),
            currency="INR",
            opened_at=datetime(2026, 3, 1, tzinfo=IST),
            closed_at=datetime.combine(second, time(10, 0), tzinfo=IST),
            state="Closed",
            rules_fired={},
        )
        session.add(position)
        session.flush()
        session.add(
            HatTradeResult(
                position_id=position.id,
                realized_pnl=-loss,
                fees=D(0),
                currency="INR",
                r_multiple=D(-1),
                holding_days=1,
                exit_reason="stop",
                mfe_r=D(0),
                mae_r=D(-1),
            )
        )
        session.flush()
        snapshot_bots(session, first)
        snapshot_bots(session, second)
        session.commit()
        tripped = evaluate_drawdown_kills(session, seed_policy(), AT + timedelta(days=400))
        snap = session.get(HatDailyBotSnapshot, (bot, second))
        assert snap.equity == new_capital - loss
        breach = (snap.equity_peak - snap.equity) * 100 >= 10 * snap.equity_peak
        assert (str(bot) in tripped) is breach
        assert snap.equity_peak >= new_capital
        assert snap.equity_peak <= new_capital + D("0.0001")  # restated, not the old 100%


@given(
    st.dictionaries(st.integers(0, 9), CAPITAL, max_size=6),
    st.lists(st.integers(0, 9), min_size=1, max_size=4),
)
def test_snapshot_latest_allocation(allocations: dict[int, Decimal], days: list[int]) -> None:
    """HAT-RISK-066 (PR 33): `snapshot.bots` runs on its own time, not after `allocate`. For every
    set of allocation nights and snapshot days (in any order, repeated or not), the snapshot of
    day d exists iff the bot has an allocation on or before d, and with no trade results its
    equity is the capital of the latest such allocation: a night without its own allocation (a
    failed gate) is still measured on the capital in force."""
    from datetime import date

    from app.hat.models import HatAllocationDaily

    first = date(2026, 3, 2)
    engine = memory_engine()
    with Session(engine) as session:
        bot = seed_bot(session)
        for offset, capital in allocations.items():
            session.add(
                HatAllocationDaily(
                    trade_date=first + timedelta(days=offset),
                    bot_id=bot,
                    base_weight=D("0.5"),
                    regime_mult=D(1),
                    health_mult=D(1),
                    final_weight=D("0.5"),
                    capital=capital,
                    risk_pct=D("0.0025"),
                    currency="INR",
                )
            )
        session.flush()
        for offset in days:
            snapshot_bots(session, first + timedelta(days=offset))
            snap = session.get(HatDailyBotSnapshot, (bot, first + timedelta(days=offset)))
            earlier = [k for k in allocations if k <= offset]
            assert (snap is not None) == bool(earlier)
            if earlier:
                assert snap.equity == allocations[max(earlier)]
