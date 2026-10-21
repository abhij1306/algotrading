"""Shared setup for the broker and order-submission properties (PR 13).

Everything runs on `FakeBroker` or a recording stand-in for the Fyers SDK model, on an in-memory
SQLite database. Nothing reaches a real broker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from hat.fakes.broker import FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, SdkBackedBroker, reset_circuit
from hat_support import memory_engine
from sqlalchemy.orm import Session, sessionmaker

from app.hat.execution.broker import OrderType, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.submit import OrderRequest
from app.hat.models import HatBrokerAccount, HatEvent, HatOrder, HatSignal
from app.hat.models.strategy import HatBrokerAccount as _Account  # noqa: F401
from app.hat.time import utc_now

USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYMBOLS = {"SBIN": "NSE:SBIN-EQ", "RELIANCE": "NSE:RELIANCE-EQ", "M&M": "NSE:M&M-EQ"}


def adapter_over(sdk: RecordingSdk) -> FyersHatBroker:
    reset_circuit()
    return FyersHatBroker(SdkBackedBroker(sdk), symbols=lambda: list(SYMBOLS))


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
    reset_circuit()
    fake = fake if fake is not None else FakeBroker()
    guard = ExecutionGuard(reconciled=reconciled)
    return ExecEnv(
        session=session,
        account_id=account.id,
        fake=fake,
        broker=FyersHatBroker(fake, symbols=lambda: list(SYMBOLS)),  # type: ignore[arg-type]
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
