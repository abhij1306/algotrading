"""DB checks of the risk services against Postgres:
the real policy, kill and snapshot code writes rows the database constraints accept, and a
service that skips its own rule is stopped by the database."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import hat_db_builders as b
import pytest
from hat_risk_support import TRADE_DAY, calendar_around, policy_with
from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from app.hat.configs.records import RiskPolicyVersion
from app.hat.configs.unit import configs_of
from app.hat.models import (
    HatAllocationDaily,
    HatDailyBotSnapshot,
    HatEvent,
    HatKillSwitch,
)
from app.hat.risk.drawdown import evaluate_drawdown_kills, snapshot_bots
from app.hat.risk.kill_ops import rearm_kill, trip_kill
from app.hat.risk.policy import seed_policy
from app.hat.risk.policy_service import (
    apply_due_versions,
    request_policy_change,
    seed_initial_policy,
)
from app.hat.time import IST

pytestmark = pytest.mark.postgres

EVENING = b.NOW.astimezone(IST).replace(hour=20, minute=0)
EVIDENCE = {
    "live_trades": 60,
    "expectancy_r": "0.4",
    "ci90_low": "0.1",
    "ci90_high": "0.7",
    "backtest_expectancy_r": "0.3",
}


def session_on(conn: Connection) -> Session:
    return b.session(conn, join_transaction_mode="create_savepoint")


def test_risk__policy_service_rows_pass_the_database(conn: Connection) -> None:
    """The policy service's versions (seed, a decrease, an increase) and their event rows: every
    request and effect has its event row."""
    user = uuid.uuid4()
    calendar = calendar_around(TRADE_DAY, span=40)
    with session_on(conn) as session:
        seed_initial_policy(session, user, EVENING - timedelta(days=1))
        down = request_policy_change(
            session,
            user_id=user,
            proposed=policy_with(risk_pct="0.002"),
            calendar=calendar,
            requested_at=EVENING,
            chat_id=1,
        )
        up = request_policy_change(
            session,
            user_id=user,
            proposed=policy_with(risk_pct="0.0030"),
            calendar=calendar,
            requested_at=EVENING,
            evidence=lambda _s, _u, _at: EVIDENCE,
            chat_id=1,
        )
        apply_due_versions(session, user, EVENING + timedelta(days=30))
        session.flush()
        assert (down.direction, up.direction) == ("decrease", "increase")
        assert up.effective_from - up.requested_at >= timedelta(days=7)
        rows = configs_of(session).all(RiskPolicyVersion, lambda r: r.user_id == user)
        assert len(rows) == 3
        kinds = [
            e.event_type for e in session.scalars(select(HatEvent).where(HatEvent.user_id == user))
        ]
        assert kinds.count("risk.change_requested") == 3
        assert kinds.count("risk.change_effective") == 3


def test_risk__kill_rows_pass_the_scope_check(conn: Connection) -> None:
    """Trips and re-arms of the account and of a bot are rows the scope check accepts."""
    w = b.world(conn)
    with session_on(conn) as session:
        trip_kill(session, scope="account", bot_id=None, reason="manual", now=b.NOW)
        trip_kill(session, scope="bot", bot_id=w["bot"], reason="drawdown_bot", now=b.NOW)
        rearm_kill(session, scope="bot", bot_id=w["bot"], confirmed=True, now=b.later(1), chat_id=1)
        rows = session.scalars(select(HatKillSwitch)).all()
        assert sorted((r.scope, r.state) for r in rows) == [
            ("account", "tripped"),
            ("bot", "armed"),
            ("bot", "tripped"),
        ]


def test_risk__snapshot_job_is_idempotent_and_feeds_the_kill(conn: Connection) -> None:
    """`snapshot.bots` writes one row per bot and day however often it runs; equity is allocation
    capital plus the realised P&L the funds read has not yet carried (Q1), so a cut of weight
    with no trade is a reallocation and trips nothing, while a realised loss after the last
    allocation, 10% under the restated peak, trips the bot kill and, the world holding this one
    bot, the account kill with it (both 10%, D128)."""
    w = b.world(conn)
    day = date(2026, 10, 19)
    sig = b.signal(conn, w)
    pos = b.position(
        conn,
        w,
        sig,
        is_virtual=False,
        qty_open=0,
        state="Closed",
        closed_at=datetime.combine(day + timedelta(days=2), time(10, 0), tzinfo=IST),
    )
    b.trade_result(conn, pos, realized_pnl=Decimal("-9000"), r_multiple=Decimal("-1"))
    with session_on(conn) as session:
        for offset, weight, capital in ((0, "0.5", "100000"), (1, "0.445", "89000")):
            session.add(
                HatAllocationDaily(
                    trade_date=day + timedelta(days=offset),
                    bot_id=w["bot"],
                    base_weight=Decimal("0.5"),
                    regime_mult=Decimal(1),
                    health_mult=Decimal(1),
                    final_weight=Decimal(weight),
                    capital=Decimal(capital),
                    risk_pct=Decimal("0.0025"),
                    currency="INR",
                )
            )
        session.flush()
        for offset in (0, 1):
            for _ in range(2):
                snapshot_bots(session, day + timedelta(days=offset))
        snaps = session.scalars(
            select(HatDailyBotSnapshot).where(HatDailyBotSnapshot.bot_id == w["bot"])
        ).all()
        assert sorted((s.trade_date, s.equity, s.equity_peak) for s in snaps) == [
            (day, Decimal("100000"), Decimal("100000")),
            (day + timedelta(days=1), Decimal("89000"), Decimal("89000")),
        ]
        assert evaluate_drawdown_kills(session, seed_policy(), b.later(5)) == []
        for _ in range(2):
            snapshot_bots(session, day + timedelta(days=2))
        loss = session.get(HatDailyBotSnapshot, (w["bot"], day + timedelta(days=2)))
        assert (loss.equity, loss.equity_peak) == (Decimal("80000"), Decimal("89000"))
        tripped = evaluate_drawdown_kills(session, seed_policy(), b.later(6))
        assert sorted(tripped) == sorted([str(w["bot"]), "account"])


def test_risk__the_peak_carries_across_a_manual_rearm(conn: Connection) -> None:
    """QA10 (owner 2026-10-25): 9% under the peak, a manual kill re-armed, then another 9%
    trips the 10% bot kill (and the account's, the world holding this one bot): the re-arm
    leaves the snapshot peak, which the health multiplier reads too, where it was. Each fall is
    a fall of the funds total at the same weight, a result (Q1)."""
    w = b.world(conn)
    day = date(2026, 10, 19)
    with session_on(conn) as session:
        for offset, capital in ((0, "100000"), (1, "91000"), (2, "82810")):
            session.add(
                HatAllocationDaily(
                    trade_date=day + timedelta(days=offset),
                    bot_id=w["bot"],
                    base_weight=Decimal("0.5"),
                    regime_mult=Decimal(1),
                    health_mult=Decimal(1),
                    final_weight=Decimal("0.5"),
                    capital=Decimal(capital),
                    risk_pct=Decimal("0.0025"),
                    currency="INR",
                )
            )
        session.flush()
        snapshot_bots(session, day)
        snapshot_bots(session, day + timedelta(days=1))
        evening = datetime.combine(day + timedelta(days=1), time(20, 0), tzinfo=IST)
        assert evaluate_drawdown_kills(session, seed_policy(), evening) == []
        trip_kill(session, scope="bot", bot_id=w["bot"], reason="manual", now=evening)
        later = evening + timedelta(minutes=1)
        rearm_kill(session, scope="bot", bot_id=w["bot"], confirmed=True, now=later, chat_id=1)
        snapshot_bots(session, day + timedelta(days=2))
        after = session.get(HatDailyBotSnapshot, (w["bot"], day + timedelta(days=2)))
        assert (after.equity, after.equity_peak) == (Decimal("82810"), Decimal("100000"))
        night = datetime.combine(day + timedelta(days=2), time(20, 0), tzinfo=IST)
        tripped = evaluate_drawdown_kills(session, seed_policy(), night)
        assert sorted(tripped) == sorted([str(w["bot"]), "account"])
