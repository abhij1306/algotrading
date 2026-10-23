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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.hat.data.sessions import IST
from app.hat.models import (
    HatAllocationDaily,
    HatDailyBotSnapshot,
    HatEvent,
    HatKillSwitch,
    HatRiskPolicyVersion,
)
from app.hat.risk import policy_service
from app.hat.risk.controls import evaluate_drawdown_kills, rearm_kill, snapshot_bots, trip_kill
from app.hat.risk.policy import CoolingOffError, seed_policy
from app.hat.risk.policy_service import (
    apply_due_versions,
    request_policy_change,
    seed_initial_policy,
)

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
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def test_risk__policy_service_rows_pass_the_database(conn: Connection) -> None:
    """The version rows the policy service writes (seed, a decrease, an increase) satisfy the
    cooling-off check, and every request and effect has its event row."""
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
        rows = session.scalars(
            select(HatRiskPolicyVersion).where(HatRiskPolicyVersion.user_id == user)
        ).all()
        assert len(rows) == 3
        kinds = [
            e.event_type for e in session.scalars(select(HatEvent).where(HatEvent.user_id == user))
        ]
        assert kinds.count("risk.change_requested") == 3
        assert kinds.count("risk.change_effective") == 3


def test_risk__policy_service_without_cooling_off_is_rejected(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken variant: a service with zero cooling-off cannot write an increase; the model guard or
    database check refuses it."""
    monkeypatch.setattr(policy_service, "COOLING_OFF", timedelta(0))
    user = uuid.uuid4()
    with session_on(conn) as session:
        seed_initial_policy(session, user, EVENING - timedelta(days=1))
        proposed = policy_with(risk_pct="0.0030")
        calendar = calendar_around(TRADE_DAY, span=40)
        with pytest.raises((CoolingOffError, IntegrityError)):
            request_policy_change(
                session,
                user_id=user,
                proposed=proposed,
                calendar=calendar,
                requested_at=EVENING,
                evidence=lambda _s, _u, _at: EVIDENCE,
                chat_id=1,
            )


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
    capital plus realised P&L, so a cut of capital with no trade is a reallocation and trips
    nothing, while a realised loss 10% under the restated peak trips the bot kill
    and, the world holding this one bot, the account kill with it (both 10%, D128)."""
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
        for offset, capital in ((0, "100000"), (1, "89000"), (2, "89000")):
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
