"""A Telegram rig for the PR 16 properties: the real service, relay, notifier and reports over a
recording fake of the Bot API transport, one confirm-mode bot on one broker account, the real
router over the Fyers adapter and `FakeBroker`, and SQLite in memory.

Nothing reaches Telegram or a broker: `FakeTransport` stands in for the HTTP layer and records
every Bot API call; `FakeBroker` records every broker call.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from hat.fakes.broker import FakeBroker
from hat_engine_support import AT, TRADE_DAY, World, request_for, seed_engine_world
from hat_exec_support import (
    GTT_READY,
    SYMBOLS,
    confirm_gates,
    guard_on,
    live_session,
    seed_live_sessions,
)
from hat_risk_support import CALENDAR
from hat_support import memory_engine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.engine import trades
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.manage import FlattenReport
from app.hat.execution.reconcile import ReconciliationService, notifier_sink
from app.hat.execution.router import ExecutionRouter, PlacementCheck
from app.hat.models import (
    HatBot,
    HatBrokerAccount,
    HatCommandLog,
    HatInstrument,
    HatMarket,
    HatStrategy,
    HatTradingCalendar,
)
from app.hat.notify import Notifier, RecordingNotifier
from app.hat.settings import load_settings, process_env
from app.hat.telegram.callbacks import RouterDesk
from app.hat.telegram.client import BotClient, TelegramError
from app.hat.telegram.confirm import PendingConfirmations
from app.hat.telegram.notifier import TelegramNotifier
from app.hat.telegram.service import TelegramService
from app.hat.telegram.tokens import ApprovalSigner
from app.hat.time import IST, as_utc

__all__ = [
    "AT",
    "CHAT",
    "OWNER",
    "LIVE",
    "OpenFlattener",
    "SECRET",
    "TRADE_DAY",
    "FakeTransport",
    "TgRig",
    "BOT_USERNAME",
    "button_update",
    "ist",
    "text_update",
]

D = Decimal
CHAT = 424242
BOT_USERNAME = "Hat_Test_Bot"
SECRET = bytes(range(32, 64))  # a test key of 32 bytes; the real one comes from the environment
LIVE_LTP = D(10**6)  # a fresh live price above every stop drawn here (PR 51)
LIVE = PlacementCheck(CALENDAR, feed_stale=False, quality_failed=False, gate_on=True, ltp=LIVE_LTP)
_TICKERS = tuple(SYMBOLS)
OWNER = load_settings(process_env()).user_id
BOT = "trend"  # the rig's bot, by its strategy's name: `/mode BOT auto`


def ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


class FakeTransport:
    """The Bot API, faked: records every call; `getUpdates` returns the queued updates from the
    offset on; `sendMessage` numbers its messages; `getMe` names the bot `BOT_USERNAME`; a method
    in `failing` raises."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queued: list[dict[str, Any]] = []
        self.failing: set[str] = set()
        self._next = 1000

    def call(self, method: str, payload: Mapping[str, Any], timeout: float) -> object:
        self.calls.append((method, dict(payload)))
        if method in self.failing:
            raise TelegramError("telegram_transport")
        if method == "getUpdates":
            return [u for u in self.queued if u["update_id"] >= payload["offset"]]
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": BOT_USERNAME}
        if method == "sendMessage":
            self._next += 1
            return {"message_id": self._next}
        return True

    def named(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def texts(self) -> list[str]:
        return [payload["text"] for payload in self.named("sendMessage")]

    def outbound(self) -> list[tuple[str, dict[str, Any]]]:
        return [(name, payload) for name, payload in self.calls if name != "getUpdates"]


def text_update(update_id: int, chat_id: int, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def button_update(update_id: int, chat_id: int, data: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def _seed_calendar(session: Session, market_id: uuid.UUID) -> None:
    for day, row in CALENDAR.days.items():
        session.add(
            HatTradingCalendar(
                market_id=market_id,
                day=day,
                is_trading=row.is_trading,
                # SQLite keeps no offset: store UTC, as Postgres hands it back
                open_at=as_utc(row.open_at) if row.open_at else None,
                close_at=as_utc(row.close_at) if row.close_at else None,
            )
        )


class OpenFlattener:
    """A flattener with no positions whose session is open unless `open` is cleared (the real
    `ManagerFlattener` over `TradeManager` runs in `test_flatten_logged`)."""

    def __init__(self) -> None:
        self.open = True
        self.calls = 0

    def orders_open(self, now: datetime) -> bool:
        return self.open

    def flatten(self, session: Session, now: datetime) -> FlattenReport:
        self.calls += 1
        return FlattenReport()


class TgRig:
    """One confirm bot on one active account with GTT and up to three instruments, its
    calendar, the router over `FakeBroker`, and the Telegram service over `FakeTransport`."""

    def __init__(
        self,
        *,
        mode: str = "confirm",
        clock: Callable[[], datetime] | None = None,
        price: Decimal | None = None,
        secret: bytes = SECRET,
        fake: FakeBroker | None = None,
        notifier: Notifier | None = None,
        telegram_alerts: bool = False,
        instruments: int = 2,
    ) -> None:
        self.engine = memory_engine()
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.now = AT
        self.price = D(100) if price is None else price
        self.locked: bool | None = False
        with self.factory() as session:
            self.world: World = seed_engine_world(session, mode=mode, instruments=instruments)
            bot = session.get(HatBot, self.world.bot_id)
            assert bot is not None
            self.account_id = bot.broker_account_id
            market_id = session.scalar(select(HatMarket.id))
            assert market_id is not None
            session.add(
                HatBrokerAccount(
                    id=self.account_id,
                    user_id=OWNER,  # the configured HAT_USER_ID: auto is allowed on it
                    broker="fyers",
                    account_ref="X0001",
                    capabilities=GTT_READY.to_json(),
                    status="active",
                )
            )
            for n, instrument in enumerate(self.world.instruments):
                session.add(
                    HatInstrument(
                        id=instrument,
                        market_id=market_id,
                        isin=f"INE{n:09d}",
                        series="EQ",
                        symbol=_TICKERS[n],
                        name=_TICKERS[n],
                        face_value=D(1),
                        active=True,
                    )
                )
            _seed_calendar(session, market_id)
            seed_live_sessions(session, self.account_id)
            session.get_one(HatStrategy, self.world.strategy_id).name = BOT
            session.flush()
            confirm_gates(session, self.account_id, OWNER)
            session.commit()
        self.fake = fake if fake is not None else FakeBroker()
        self.transport = FakeTransport()
        self.client = BotClient(self.transport, CHAT)
        broker = FyersHatBroker(
            self.fake,  # type: ignore[arg-type]
            instruments=lambda: list(SYMBOLS.items()),
            capabilities=GTT_READY,
        )
        live_session(SimpleNamespace(broker=broker))  # type: ignore[arg-type]
        self.notifier: Notifier = notifier if notifier is not None else RecordingNotifier()
        if telegram_alerts:
            self.notifier = TelegramNotifier(self.client, self.factory)
        guard = guard_on(TRADE_DAY)
        reconciler = ReconciliationService(broker, guard, notifier_sink(self.notifier))
        self.signer = ApprovalSigner(secret)
        self.router = ExecutionRouter(
            broker, guard, reconciler, self.notifier, issuer=self.signer.issue
        )
        self.confirmations = PendingConfirmations()
        self.desk = RouterDesk(
            lambda account: self.router if account == self.account_id else None,
            lambda _session, _signal, _now: (self.price, self.locked, LIVE),
        )
        self.flattener = OpenFlattener()
        self.service = TelegramService(
            self.client,
            self.factory,
            signer=self.signer,
            confirmations=self.confirmations,
            desk=self.desk,
            flattener=self.flattener,
            clock=clock or (lambda: self.now),
        )

    def route(
        self, instrument: int = 0, at: datetime = AT, confidence: str | None = "high", **ctx: object
    ) -> trades.EntryOutcome:
        with self.factory() as session:
            request = request_for(self.world, self.world.instruments[instrument], **ctx)
            outcome = self.router.route(
                session,
                trades.EntryRequest(**{**vars(request), "triggered_at": at}),
                now=at,
                check=LIVE,
                confidence=confidence,
            )
            session.commit()
            return outcome

    def log(self) -> list[HatCommandLog]:
        with self.factory() as session:
            return list(session.scalars(select(HatCommandLog).order_by(HatCommandLog.id)))

    def places(self) -> list[dict]:
        return self.fake.calls_named("place_order")

    def at(self, moment: datetime) -> TgRig:
        self.now = moment
        return self

    def later(self, seconds: float) -> datetime:
        return self.now + timedelta(seconds=seconds)
