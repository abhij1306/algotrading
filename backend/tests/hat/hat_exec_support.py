"""Shared setup for the broker and order-submission properties (PR 13).

Everything runs on `FakeBroker` or a recording stand-in for the Fyers SDK model, on an in-memory
SQLite database. Nothing reaches a real broker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from hat.fakes.broker import FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from hat_support import memory_engine
from sqlalchemy.orm import Session, sessionmaker

from app.hat.execution.broker import OrderType, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.submit import OrderRequest
from app.hat.models import HatBrokerAccount, HatBrokerSession, HatEvent, HatOrder, HatSignal
from app.hat.models.strategy import HatBrokerAccount as _Account  # noqa: F401
from app.hat.time import utc_now

USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYMBOLS = {"SBIN": "NSE:SBIN-EQ", "RELIANCE": "NSE:RELIANCE-EQ", "M&M": "NSE:M&M-EQ"}


def adapter_over(sdk: RecordingSdk) -> FyersHatBroker:
    return FyersHatBroker(broker_over(sdk), instruments=lambda: list(SYMBOLS.items()))


@dataclass
class ExecEnv:
    """A database with one active account, the fake broker beneath the adapter, and a guard."""

    session: Session
    account_id: uuid.UUID
    fake: FakeBroker
    broker: FyersHatBroker
    guard: ExecutionGuard
    bot_id: uuid.UUID

    def request(
        self,
        signal_id: uuid.UUID,
        purpose: str = "entry",
        *,
        qty: int = 10,
        symbol: str = "SBIN",
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> OrderRequest:
        side = Side.BUY if purpose == "entry" else Side.SELL
        return OrderRequest(
            bot_id=self.bot_id,
            signal_id=signal_id,
            broker_account_id=self.account_id,
            purpose=purpose,
            side=side,
            qty=qty,
            order_type=order_type,
            symbol=symbol,
            price=price,
            trigger_price=trigger_price,
        )


def exec_env(
    *, account_status: str = "active", reconciled: bool = True, fake: FakeBroker | None = None
) -> ExecEnv:
    engine = memory_engine(HatEvent, HatOrder, HatBrokerAccount, HatSignal)
    session = sessionmaker(engine, expire_on_commit=False)()
    account = HatBrokerAccount(
        user_id=USER,
        broker="fyers",
        account_ref="X0001",
        capabilities={},
        status=account_status,
    )
    session.add(account)
    session.commit()
    fake = fake if fake is not None else FakeBroker()
    guard = guard_on(SESSION_DAY, reconciled=reconciled)
    return ExecEnv(
        session=session,
        account_id=account.id,
        fake=fake,
        broker=FyersHatBroker(fake, instruments=lambda: list(SYMBOLS.items())),  # type: ignore[arg-type]
        guard=guard,
        bot_id=uuid.uuid4(),
    )


def live_session(env: ExecEnv) -> None:
    """Make the adapter report a live session without touching a token file."""

    class _Status:
        connected = True

    class _Auth:
        def status(self) -> _Status:
            return _Status()

    class _Client:
        fyers = object()

    env.broker._auth = lambda: _Auth()  # type: ignore[assignment,return-value]
    env.broker._client = lambda: _Client()  # type: ignore[assignment,return-value]


__all__ = [
    "SYMBOLS",
    "USER",
    "ExecEnv",
    "RecordingSdk",
    "adapter_over",
    "exec_env",
    "live_session",
    "utc_now",
]


SESSION_DAY = date(2026, 10, 21)  # the trade day of the rigs (a Wednesday)


def guard_on(day: date, *, reconciled: bool = True) -> ExecutionGuard:
    """A guard whose trade date is `day`, reconciled for that date or not."""
    return ExecutionGuard(reconciled_for=day if reconciled else None, today=lambda: day)


def seed_live_sessions(
    session: Session, account_id: uuid.UUID, around: date = SESSION_DAY, span: int = 10
) -> None:
    """A live `hat_broker_session` for the account on every day within `span` days of `around`
    (the router refuses an entry without one, HAT-OPS-023)."""
    for offset in range(-span, span + 1):
        session.add(
            HatBrokerSession(
                broker_account_id=account_id,
                trade_date=around + timedelta(days=offset),
                status="live",
            )
        )
