"""Shared setup for the broker and order-submission properties (PR 13).

Everything runs on `FakeBroker` or a recording stand-in for the Fyers SDK model, on an in-memory
SQLite database. Nothing reaches a real broker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hat_support import memory_engine
from sqlalchemy.orm import Session, sessionmaker

from app.brokers.plugins.fyers import FyersBroker
from app.hat.execution.broker import OrderType, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.submit import OrderRequest
from app.hat.models import HatBrokerAccount, HatEvent, HatOrder, HatSignal
from app.hat.models.strategy import HatBrokerAccount as _Account  # noqa: F401
from app.hat.time import utc_now

from fakes.broker import FakeBroker

USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYMBOLS = {"SBIN": "NSE:SBIN-EQ", "RELIANCE": "NSE:RELIANCE-EQ", "M&M": "NSE:M&M-EQ"}


class RecordingSdk:
    """A fake fyers_apiv3 FyersModel: records every payload it is sent, answers from `reply`."""

    def __init__(self, reply: Any = None) -> None:  # noqa: ANN401 - raw SDK JSON
        self.calls: list[tuple[str, Any]] = []
        self.reply = reply

    def _answer(self, name: str, data: Any) -> Any:  # noqa: ANN401
        self.calls.append((name, data))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if self.reply is not None:
            return self.reply
        return {"s": "ok", "code": 200, "id": f"SDK-{len(self.calls)}", "message": "ok"}

    def place_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("place_order", data)

    def modify_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("modify_order", data)

    def cancel_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("cancel_order", data)

    def place_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("place_gtt_order", data)

    def modify_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("modify_gtt_order", data)

    def cancel_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("cancel_gtt_order", data)

    def orderbook(self) -> Any:  # noqa: ANN401
        return self._answer("orderbook", None)

    def tradebook(self) -> Any:  # noqa: ANN401
        return self._answer("tradebook", None)

    def holdings(self) -> Any:  # noqa: ANN401
        return self._answer("holdings", None)

    def funds(self) -> Any:  # noqa: ANN401
        return self._answer("funds", None)

    def positions(self) -> Any:  # noqa: ANN401
        return self._answer("positions", None)

    def gtt_orderbook(self) -> Any:  # noqa: ANN401
        return self._answer("gtt_orderbook", None)


class SdkBackedBroker(FyersBroker):
    """The real `FyersBroker` over a `RecordingSdk` instead of the singleton client."""

    def __init__(self, sdk: RecordingSdk) -> None:
        self._sdk = sdk

    @property
    def client(self) -> Any:  # noqa: ANN401
        return self._sdk


def reset_circuit() -> None:
    """The breaker is class state: put it back to closed between examples."""
    FyersBroker._record_success()


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
