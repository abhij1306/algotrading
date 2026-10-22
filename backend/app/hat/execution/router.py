"""The execution router: one risk check, three routes (07 §1-§3, §11; HAT-EXEC-010, -012, -020,
-022, -023, -030..032, HAT-BRK-110; the model is `verification/tla/ExecutionRouter.tla`).

A trigger reaches the router as an `EntryRequest`. The router works out the bot's effective mode
for the session (`modes.bot_mode`: the queued mode in force at the session open, gated by the
account's GTT capability) and hands the request to `trades.open_entry`, which runs
`evaluate_signal` under the bot's lock and the owner's (over every bot's slots, HAT-RISK-030) and
writes what the mode needs in one transaction:

- **shadow**: the virtual position (no broker call, HAT-EXEC-010);
- **confirm**: the approval card (AwaitingApproval). The user's Take re-checks max-chase
  (`approvals.take`) and the live conditions below and, when both pass, the router places the
  real order; a Skip or an expiry continues as a virtual veto trade (HAT-EXEC-032);
- **auto**: the accepted signal waits in Triggered and the router places the real order now.

**Placing a real entry** (`place_entry`) first re-reads the live conditions (`PlacementCheck`):
no entry outside the calendar session, on a stale or unknown feed, on a day whose data failed
the quality gate, or while the regime gate is off (HAT-EXEC-022, HAT-RISK-074, -075, -050), and
none once the account no longer reports GTT for delivery (HAT-EXEC-012). The risk check ran
them at the trigger; a Take minutes later, or an auto entry resumed after a crash, reads them
again. The entry then goes only through `submit.reserve_order` and
`submit.send_order` (the one path to the broker, trading-safety rule 1). Before the session's
first order the broker book is reconciled (HAT-BRK-110). A rejected entry is alerted and retried
at most once with the next tag (HAT-EXEC-023); a second rejection, a refusal on the order path or
an unreachable broker ends the signal Rejected with a fixed reason and an alert. An order whose
outcome is unknown is left to the tag lookup and reconciliation, never re-placed blindly.

The router imports only `execution.broker`'s contract, never the Fyers adapter or SDK (CONF
`brk.import_boundary`). The Fyers adapter reports no GTT for delivery, so in the MVP every bot's
effective mode is shadow and the real-order path runs only against `FakeBroker`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..engine import approvals, lifecycle, trades
from ..models import (
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatInstrument,
    HatOrder,
    HatSignal,
)
from ..notify import LogNotifier, Notifier
from ..risk import controls
from ..risk.decision import Reason, SignalContext
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar, SessionState
from ..time import trade_date_of
from .broker import BrokerUnavailableError, HatBroker, OrderType, SessionStatus, Side
from .guard import ExecutionGuard
from .modes import AUTO, bot_mode, effective_mode
from .reconcile import ReconciliationService
from .submit import (
    OrderRefusedError,
    OrderRequest,
    SendOutcome,
    UnresolvedOrderError,
    reserve_order,
    send_order,
)

__all__ = [
    "ALERT_ENTRY_NOT_PLACED",
    "ALERT_ORDER_REJECTED",
    "MAX_ENTRY_REJECTIONS",
    "ExecutionRouter",
    "PlacementCheck",
    "placement_refusal",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
# HAT-EXEC-023: a rejected entry is retried once, so at most two rejections end it.
MAX_ENTRY_REJECTIONS = 2
ALERT_ORDER_REJECTED = "order.rejected"
ALERT_ENTRY_NOT_PLACED = "entry.not_placed"
NO_GTT = "no_gtt_delivery"
SESSION_NOT_LIVE = "broker_session_not_live"  # HAT-OPS-023
SIGNAL_EXPIRED = "signal_expired"  # a Triggered entry of an earlier session (HAT-OPS-003)
_OPEN_STATUSES = ("pending", "unconfirmed", "placed", "partially_filled", "filled")
_ROUTABLE = (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL)


@dataclass(frozen=True)
class PlacementCheck:
    """The live conditions a real entry is placed under, read at placement time. An unknown
    input (None, or no calendar) fails closed."""

    calendar: SessionCalendar | None
    feed_stale: bool | None
    quality_failed: bool | None
    gate_on: bool | None

    @classmethod
    def of(cls, ctx: SignalContext) -> PlacementCheck:
        """The conditions a trigger's own context carries."""
        return cls(ctx.calendar, ctx.feed_stale, ctx.quality_failed, ctx.gate_on)


def placement_refusal(check: PlacementCheck, now: datetime) -> str | None:
    """Why no real entry may be placed at `now` (the risk check's own reason code), or None."""
    if check.calendar is None or check.calendar.state(now) is not SessionState.OPEN:
        return Reason.MARKET_CLOSED.value
    if check.feed_stale is not False:
        return Reason.STALE_FEED.value
    if check.quality_failed is not False:
        return Reason.QUALITY_FAILED.value
    if check.gate_on is not True:
        return Reason.REGIME_GATE.value
    return None


def _kill_refusal(session: Session, bot_id: uuid.UUID) -> str | None:
    """A kill switch or a pause that came on after the risk check (the bot's lock is released
    when the signal commits) stops the entry: the bot is manage-only (06 §6)."""
    kill = controls.kill_state(session, bot_id)
    for tripped, reason in (
        (kill.account_tripped, Reason.KILL_ACCOUNT),
        (kill.bot_tripped, Reason.KILL_BOT),
        (kill.health_tripped, Reason.KILL_HEALTH),
        (kill.paused, Reason.BOT_PAUSED),
    ):
        if tripped:
            return reason.value
    return None


def _gtt_refusal(session: Session, account_id: uuid.UUID) -> str | None:
    """HAT-EXEC-012 at placement: the account must still run real orders (active, GTT for
    delivery) when a Take arrives minutes after its card went out."""
    account = session.get(HatBrokerAccount, account_id)
    if account is None or (
        effective_mode(AUTO, account_status=account.status, capabilities=account.capabilities or {})
        != AUTO
    ):
        return NO_GTT
    return None


def _session_refusal(session: Session, account_id: uuid.UUID, now: datetime) -> str | None:
    """HAT-OPS-023: no entry for an account without a live `hat_broker_session` for the trade
    date (the session watch writes it; a missing row is not live)."""
    row = session.get(HatBrokerSession, (account_id, trade_date_of(now)))
    if row is None or row.status != SessionStatus.LIVE.value:
        return SESSION_NOT_LIVE
    return None


class ExecutionRouter:
    """Routes accepted signals by mode and places real entries. One router serves one broker
    account's session; `guard` is that session's reconciliation state."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        reconciler: ReconciliationService,
        notifier: Notifier | None = None,
        issuer: trades.TokenIssuer | None = None,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._reconciler = reconciler
        self._notifier = notifier or LogNotifier()
        # Signs each approval card's token (the Telegram signer). Without one a confirm card
        # can never be taken (`trades._send_approval`).
        self._issuer = issuer

    # --- routing ------------------------------------------------------------------------------

    def route(
        self,
        session: Session,
        request: trades.EntryRequest,
        *,
        now: datetime,
        check: PlacementCheck,
        decide: trades.DECIDE = evaluate_signal,
    ) -> trades.EntryOutcome:
        """Write the trigger's signal under its effective mode and commit; place an accepted auto
        entry under `check`, the live conditions read by the caller at `now` (the placement
        time, not the trigger's snapshot). A repeated trigger returns the existing signal (and
        resumes an auto entry that a crash left unplaced)."""
        bot = session.get(HatBot, request.bot_id)
        if bot is None:
            raise trades.TradeError("unknown_bot")
        mode = bot_mode(session, bot, request.triggered_at, request.context.calendar)
        outcome = trades.open_entry(session, request, decide=decide, mode=mode, issue=self._issuer)
        session.commit()
        if outcome.mode == AUTO and outcome.state == lifecycle.TRIGGERED:
            self.place_entry(session, outcome.signal_id, now=now, check=check)
            signal = session.get(HatSignal, outcome.signal_id)
            state = signal.state if signal is not None else outcome.state
            return trades.EntryOutcome(
                outcome.signal_id,
                state,
                outcome.decision,
                None,
                duplicate=outcome.duplicate,
                mode=outcome.mode,
            )
        return outcome

    # --- confirm mode ---------------------------------------------------------------------------

    def take(
        self,
        session: Session,
        signal_id: uuid.UUID,
        *,
        price: Decimal | None,
        locked_upper_circuit: bool | None,
        now: datetime,
        chat_id: int,
        check: PlacementCheck,
    ) -> approvals.ApprovalDecision:
        """The user's Take: the decision commits first; a Take that passed the live conditions
        and the max-chase re-check is then placed as a real entry."""
        decision = approvals.take(
            session,
            signal_id,
            price=price,
            locked_upper_circuit=locked_upper_circuit,
            now=now,
            chat_id=chat_id,
            refusal=placement_refusal(check, now),
        )
        session.commit()
        if decision.place:
            self.place_entry(session, signal_id, now=now, check=check)
        return decision

    def skip(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> approvals.ApprovalDecision:
        """The user's Skip; the signal continues as a veto trade."""
        decision = approvals.skip(session, signal_id, now=now, chat_id=chat_id)
        session.commit()
        return decision

    def expire(self, session: Session, now: datetime) -> int:
        """The approval sweep: expire what is due and open the missing veto trades."""
        count = approvals.expire_due(session, now)
        session.commit()
        return count

    # --- placing a real entry -------------------------------------------------------------------

    def place_entry(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, check: PlacementCheck
    ) -> SendOutcome | None:
        """Place the real entry of an accepted auto signal or a passed Take, at the entry price
        the engine fixed at the trigger, when `check` allows an entry at `now`, no kill switch or
        pause is on and the account still runs real orders. Safe to call again: an order already
        reserved is sent (or looked up) under its own tag, and a signal that left Triggered or
        AwaitingApproval is left alone. On a refusal a reserved order that never reached the
        broker is cancelled and one that may have is left to reconciliation; nothing is sent.
        Returns the last send, or None when nothing was sent."""
        signal = session.get(HatSignal, signal_id)
        if signal is None:
            raise trades.TradeError("unknown_signal")
        if signal.state not in _ROUTABLE or signal.mode == trades.SHADOW or signal.qty <= 0:
            return None
        bot = session.get(HatBot, signal.bot_id)
        if bot is None:
            raise trades.TradeError("unknown_bot")
        account_id = bot.broker_account_id
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == signal.instrument_id)
        )
        if symbol is None:
            return self._not_placed(session, signal, "unknown_instrument", now)
        refusal = self._preflight(session, signal, account_id, now, check)
        if refusal is not None:
            tag = self._open_entry_tag(session, signal.id)
            if tag is not None and not self._release_unsent(session, tag):
                logger.warning(
                    "entry %s refused (%s) but may be placed; left to reconcile", tag, refusal
                )
                return None
            return self._not_placed(session, signal, refusal, now)
        return self._send_entry(session, signal, account_id, symbol, now)

    def _preflight(
        self,
        session: Session,
        signal: HatSignal,
        account_id: uuid.UUID,
        now: datetime,
        check: PlacementCheck,
    ) -> str | None:
        """Why the entry may not be placed now (a fixed code), or None."""
        if signal.trade_date != trade_date_of(now):
            return SIGNAL_EXPIRED  # its price, size and risk decision were for that session
        refusal = (
            placement_refusal(check, now)
            or _kill_refusal(session, signal.bot_id)
            or _gtt_refusal(session, account_id)
            or _session_refusal(session, account_id, now)
        )
        if refusal is not None:
            return refusal
        if not self._guard.reconciled:
            # HAT-BRK-110: the session's first order waits for a successful reconciliation.
            try:
                self._reconciler.run(session, account_id)
            except BrokerUnavailableError:
                return "broker_unavailable"
        return None

    def _send_entry(
        self,
        session: Session,
        signal: HatSignal,
        account_id: uuid.UUID,
        symbol: str,
        now: datetime,
    ) -> SendOutcome | None:
        """Send the entry under its open tag or a new one; a rejected entry is retried once."""
        sent: SendOutcome | None = None
        tag = self._open_entry_tag(session, signal.id)
        while True:
            if tag is None:
                refusal = self._guard.refusal(signal.bot_id, account_id, is_entry=True)
                if refusal is not None:
                    return self._not_placed(session, signal, refusal, now)
                tag = reserve_order(
                    session, self._entry_request(session, signal, account_id, symbol)
                )
            done, sent = self._send_once(session, signal, tag, symbol, now, previous=sent)
            if done:
                return sent
            tag = None  # the one retry: a new logical order, so the next tag

    def _send_once(
        self,
        session: Session,
        signal: HatSignal,
        tag: str,
        symbol: str,
        now: datetime,
        *,
        previous: SendOutcome | None,
    ) -> tuple[bool, SendOutcome | None]:
        """One send of `tag`: (finished, the outcome to return). Not finished only after a
        rejection that may still be retried."""
        try:
            sent = send_order(
                session, self._broker, self._guard, tag, symbol=symbol, bot_id=signal.bot_id
            )
        except (OrderRefusedError, BrokerUnavailableError) as exc:
            code = exc.code if isinstance(exc, OrderRefusedError) else "broker_unavailable"
            if self._release_unsent(session, tag):
                return True, self._not_placed(session, signal, code, now)
            logger.warning("entry %s outcome unknown after %s; left to reconcile", tag, code)
            return True, previous
        except UnresolvedOrderError:
            return True, previous
        if sent.status != "rejected":
            return True, sent
        self._alert(ALERT_ORDER_REJECTED, signal, {"tag": tag, "code": sent.code})
        if self._entry_rejections(session, signal.id) >= MAX_ENTRY_REJECTIONS:
            self._not_placed(session, signal, "order_rejected", now)
            return True, sent
        return False, sent

    def _entry_request(
        self, session: Session, signal: HatSignal, account_id: uuid.UUID, symbol: str
    ) -> OrderRequest:
        price = trades.entry_price_of(session, signal)
        return OrderRequest(
            bot_id=signal.bot_id,
            signal_id=signal.id,
            broker_account_id=account_id,
            purpose=ENTRY,
            side=Side.BUY,
            qty=signal.qty,
            order_type=OrderType.LIMIT,
            symbol=symbol,
            price=price,
        )

    @staticmethod
    def _open_entry_tag(session: Session, signal_id: uuid.UUID) -> str | None:
        """The tag of the signal's entry order that may still be (or have been) placed."""
        return session.scalar(
            select(HatOrder.tag)
            .where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.is_virtual.is_(False),
                HatOrder.status.in_(_OPEN_STATUSES),
            )
            .order_by(HatOrder.tag.desc())
            .limit(1)
        )

    @staticmethod
    def _entry_rejections(session: Session, signal_id: uuid.UUID) -> int:
        count = session.scalar(
            select(func.count())
            .select_from(HatOrder)
            .where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.status == "rejected",
            )
        )
        return int(count or 0)

    @staticmethod
    def _release_unsent(session: Session, tag: str) -> bool:
        """Close a reserved order that provably never reached the broker (pending, no attempt
        stamped) as cancelled; False when it may have been sent (left to reconciliation)."""
        session.rollback()
        row = session.scalar(select(HatOrder).where(HatOrder.tag == tag))
        if row is None or row.status != "pending" or row.placed_at is not None:
            return False
        row.status = "cancelled"
        session.commit()
        return True

    def _not_placed(self, session: Session, signal: HatSignal, reason: str, now: datetime) -> None:
        """End the signal Rejected with `reason` and raise one alert (fixed codes only)."""
        if trades.reject_signal(session, signal.id, reason, now):
            session.commit()
            self._alert(ALERT_ENTRY_NOT_PLACED, signal, {"reason": reason})
        else:
            session.rollback()

    def _alert(self, code: str, signal: HatSignal, detail: dict[str, str]) -> None:
        day: date = signal.trade_date or trade_date_of(signal.triggered_at)
        self._notifier.alert(
            code, day, {"bot_id": str(signal.bot_id), "signal_id": str(signal.id), **detail}
        )
