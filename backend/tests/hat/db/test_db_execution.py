"""Seams of the order path over Postgres and the real Fyers adapter on `FakeBroker`: a
manage-only withdraw cancels resting entries only (Q2), and a sale's day stop is cancelled only
once the broker shows it cancelled, else it stays and the owner is told URGENT."""

from datetime import datetime, time
from decimal import Decimal

import hat_db_builders as b
import pytest
from hat.fakes.broker import FakeBroker
from sqlalchemy import Connection
from sqlalchemy.orm import Session

from app.hat.execution.entry_cancel import EntryCancel
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.position_desk import PositionDesk
from app.hat.execution.protect import ALERT_STOP_CANCEL_UNACKED, StopDesk
from app.hat.execution.tags import encode_tag
from app.hat.models import HatOrder
from app.hat.notify import RecordingNotifier
from app.hat.risk.kill_ops import trip_kill
from app.hat.telegram.render import URGENT_ALERTS
from app.hat.time import IST

pytestmark = pytest.mark.postgres

NOW = datetime.combine(b.TRADE_DAY, time(11, 0), tzinfo=IST)


def session_on(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def adapter(fake: FakeBroker) -> FyersHatBroker:
    return FyersHatBroker(fake, instruments=lambda: [("TCS", "NSE:TCS-EQ")])  # type: ignore[arg-type]


def _real_order(conn: Connection, w: dict, signal_id: object, **values: object) -> object:
    return b.order(conn, w, signal_id, is_virtual=False, status="placed", placed_at=NOW, **values)


def test_execution__a_withdraw_cancels_resting_entries_only(conn: Connection) -> None:
    """Q2: a manual kill's withdraw (`EntryCancel.withdraw`) cancels the bot's resting entry and
    leaves its resting day stop and GTT exits alone: no cancel of either reaches the broker."""
    w = b.world(conn)
    held = b.signal(conn, w)
    position = b.position(conn, w, held, is_virtual=False)
    stop = {"side": "sell", "position_id": position, "trigger_price": Decimal("95")}
    _real_order(conn, w, held, purpose="stop_day", order_type="sl_m", broker_order_id="S-1", **stop)
    _real_order(conn, w, held, purpose="stop_gtt", order_type="gtt", broker_order_id="G-1", **stop)
    entry = b.signal(conn, w, playbook="breakout")
    _real_order(conn, w, entry, purpose="entry", broker_order_id="E-1")
    fake = FakeBroker()
    with session_on(conn) as session:
        trip_kill(session, scope="bot", bot_id=w["bot"], reason="manual", now=NOW)
        assert EntryCancel(adapter(fake)).withdraw(session, w["account"], NOW) == 1
    assert fake.calls == [("cancel_order", "E-1")]


def _stop_row(status: int, tag: str) -> dict[str, object]:
    return {
        "id": "S-1",
        "orderTag": tag,
        "symbol": "NSE:TCS-EQ",
        "side": -1,
        "qty": 10,
        "filledQty": 0,
        "status": status,
        "productType": "CNC",
        "type": 3,
    }


def _desk(fake: FakeBroker, notifier: RecordingNotifier) -> StopDesk:
    return StopDesk(
        adapter(fake),
        ExecutionGuard(today=lambda: b.TRADE_DAY),
        positions=PositionDesk(),
        tick_for=lambda _s, _p: Decimal("0.05"),
        notifier=notifier,
        sleep=lambda _s: None,
    )


def test_execution__a_sales_stop_is_cancelled_only_once_the_broker_shows_it(
    conn: Connection,
) -> None:
    """Stop-to-market is cancel, then sell: `make_room` frees the shares only once the book
    shows the day stop cancelled. With no acknowledgement in the bounded wait it holds the sale
    back, keeps the stop working on HAT's book and raises URGENT `stop.cancel_unacked`; nothing
    is placed either way (the sale is the caller's, after a True)."""
    w = b.world(conn)
    held = b.signal(conn, w)
    position = b.position(conn, w, held, is_virtual=False)
    tag = encode_tag(held, "stop_day", 0)
    row_id = _real_order(
        conn,
        w,
        held,
        purpose="stop_day",
        side="sell",
        order_type="sl_m",
        broker_order_id="S-1",
        position_id=position,
        trigger_price=Decimal("95"),
        tag=tag,
    )
    fake = FakeBroker(orders=[_stop_row(6, tag)])
    notifier = RecordingNotifier()
    with session_on(conn) as session:
        desk = _desk(fake, notifier)
        assert not desk.make_room(session, position, 0, NOW)
        row = session.get(HatOrder, row_id)
        assert row is not None
        assert row.status == "placed"
        assert [code for code, _, _ in notifier.alerts] == [ALERT_STOP_CANCEL_UNACKED]
        assert ALERT_STOP_CANCEL_UNACKED in URGENT_ALERTS
        assert ("cancel_order", "S-1") in fake.calls
        assert not any(name.startswith("place") for name, _ in fake.calls)

        fake.orders = [_stop_row(1, tag)]
        assert desk.make_room(session, position, 0, NOW)
        session.refresh(row)
        assert row.status == "cancelled"
        assert not any(name.startswith("place") for name, _ in fake.calls)
