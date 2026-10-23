"""Shared setup for the broker and order-submission properties.

Everything runs on `FakeBroker` or a recording stand-in for the Fyers SDK model, on an in-memory
SQLite database. Nothing reaches a real broker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

from hat.fakes.broker import FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from sqlalchemy.orm import Session

from app.hat.execution.broker import OrderType, Side
from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.submit import OrderRequest
from app.hat.models.strategy import HatBrokerAccount as _Account  # noqa: F401
from app.hat.time import utc_now

USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYMBOLS = {"SBIN": "NSE:SBIN-EQ", "RELIANCE": "NSE:RELIANCE-EQ", "M&M": "NSE:M&M-EQ"}
# The adapter's capabilities with GTT for delivery (verified live): the rigs that place
# real entries store them on the account and build the adapter with them, and `replace` them to
# turn it off.
GTT_READY = replace(FYERS_CAPABILITIES, gtt_delivery=True)


def adapter_over(sdk: RecordingSdk) -> FyersHatBroker:
    return FyersHatBroker(
        broker_over(sdk), instruments=lambda: list(SYMBOLS.items()), capabilities=GTT_READY
    )


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
    "live_session",
    "utc_now",
]
