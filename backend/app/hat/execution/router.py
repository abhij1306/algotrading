"""The execution router: one risk check, three routes (HAT-EXEC-010, -012, -020,
-022, -023, -030..032, HAT-BRK-110; the model is `verification/tla/ExecutionRouter.tla`).

A trigger reaches the router as an `EntryRequest`. The router works out the bot's effective mode
for the session (`modes.bot_mode`: the queued mode in force at the session open, gated by the
account's GTT capability) and hands the request to `entries.open_entry`, which runs
`evaluate_signal` under the bot's lock and the owner's (over every bot's slots, HAT-RISK-030) and
writes what the mode needs in one transaction:

- **shadow**: the virtual position (no broker call, HAT-EXEC-010);
- **confirm**: the approval card (AwaitingApproval). The user's Take re-checks max-chase
  (`approvals.take`) and the live conditions below and, when both pass, the router places the
  real order; a Skip or an expiry continues as a virtual veto trade (HAT-EXEC-032);
- **auto**: the accepted signal waits in Triggered and the router places the real order now. Only
  a high-confidence signal goes this way; any other is routed as confirm (D149).

**Placing a real entry** (`place_entry`) first re-reads the live conditions (`PlacementCheck`):
no entry outside the calendar session, on a stale or unknown feed, on a day whose data failed
the quality gate, or while the regime gate is off (HAT-EXEC-022, HAT-RISK-074, -075, -050),
none without a live price that is fresh and inside the day's band, or at or below the entry's
stop (PR 51: a failed breakout Taken late must not buy under its own stop), none once the
account no longer reports GTT for delivery (HAT-EXEC-012), and none once the bot no longer runs
the mode the entry was routed in (`modes.mode_refusal`: DDPI revoked, the sleeve not Live, the
weights not confirmed). The risk check ran
them at the trigger; a Take minutes later, or an auto entry resumed after a crash, reads them
again. The entry then goes only through `submit.reserve_order` and
`submit.send_order` (the one path to the broker, trading-safety rule 1). Before the session's
first order the broker book is reconciled (HAT-BRK-110). A rejected entry is alerted and retried
at most once with the next tag (HAT-EXEC-023); a second rejection, a refusal on the order path or
an unreachable broker ends the signal Rejected with a fixed reason and an alert. An order whose
outcome is unknown is left to the tag lookup and reconciliation, never re-placed blindly.

**Crash safety** (PR 52, audit T-F4, T-F7): the signal commits before its entry is placed (an auto
signal in Triggered, a Take's decision), so a crash, or a placement that raised, can fall in
between. The rejection count is read from the stored orders before each new tag, so no restart
sends a third entry order. A placement that raised is remembered (`take_stranded`) and the
restart's recovery re-reads the auto signals left in Triggered and the Takes left unplaced
(`daily/accounts.py:Desks.resume_entries`); each is placed once, under the live conditions read
then. The approval sweep (`expire`) ends Rejected `signal_expired` an entry of an earlier trade
date left without an order, so a stranded signal never holds its slot past its own day.

The router imports only `execution.broker`'s contract, never the Fyers adapter or SDK (CONF
`brk.import_boundary`). A bot runs confirm or auto only once its sleeve is Live, its account
reports GTT for delivery and has DDPI confirmed, and the owner has confirmed the policy's base
weights (`modes.gate_refusal`); auto places only a high-confidence signal (`modes.route_mode`).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from time import monotonic

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..configs.records import Bot, BrokerAccount
from ..configs.unit import configs_of
from ..data.live import PRICE_UNKNOWN
from ..engine import approvals, entries, lifecycle, positions, snapshot
from ..governance import Mode
from ..models import (
    HatApproval,
    HatBrokerSession,
    HatInstrument,
    HatOrder,
    HatSignal,
)
from ..notify import LogNotifier, Notifier
from ..risk import kill_ops
from ..risk.decision import Reason
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar, SessionState
from ..time import as_utc, trade_date_of
from .broker import BrokerUnavailableError, HatBroker, OrderType, SessionStatus, Side
from .entry_timeout import BREAK_FAILED, break_failed
from .guard import ExecutionGuard
from .modes import (
    HIGH,
    MODE_GATE_CLOSED,
    NO_GTT_DELIVERY,
    NOT_HIGH_CONFIDENCE,
    bot_mode,
    mode_refusal,
    mode_view,
    real_orders_allowed,
)
from .reconcile import ReconciliationService
from .submit import (
    OrderRefusedError,
    OrderRequest,
    OrderSender,
    SendOutcome,
    UnresolvedOrderError,
    reserve_order,
    send_order,
)

__all__ = [
    "ALERT_ENTRY_NOT_PLACED",
    "ALERT_ENTRY_UNRESOLVED",
    "ALERT_ORDER_REJECTED",
    "MAX_ENTRY_REJECTIONS",
    "PRICE_AT_OR_BELOW_STOP",
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
ALERT_ENTRY_UNRESOLVED = "entry.unresolved"  # HAT-BRK-102: never sent again, left to the sync
SESSION_NOT_LIVE = "broker_session_not_live"  # HAT-OPS-023
SIGNAL_EXPIRED = "signal_expired"  # a Triggered entry of an earlier session (HAT-OPS-003)
PRICE_AT_OR_BELOW_STOP = "price_at_or_below_stop"  # the live price is through the entry's stop
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
    # the symbol's live price, already refused when stale or outside the day's band
    # (`data.live.usable_price`, PR 51), and why it is None
    ltp: Decimal | None
    ltp_refusal: str | None = None
    # the instrument's final 5-minute closes of the day, (final instant, close); none known
    # (no bars, even after a restart's restore: Q7) fails open
    closes: tuple[tuple[datetime, Decimal], ...] = ()
    # the same check read again at a later instant, right before the send (after a
    # reconciliation's broker round trip); None keeps this one
    reread: Callable[[datetime], PlacementCheck] | None = field(default=None, compare=False)


def placement_refusal(
    check: PlacementCheck, now: datetime, signal: HatSignal | None = None
) -> str | None:
    """Why no real entry may be placed at `now` (the risk check's own reason code, or the live
    price's), or None. With the entry's `signal`, a live price at or below its stop refuses
    too, and so does a failed break: a final bar after its trigger bar closed below its level
    (`BREAK_FAILED`, Q7)."""
    if check.calendar is None or check.calendar.state(now) is not SessionState.OPEN:
        return Reason.MARKET_CLOSED.value
    if check.feed_stale is not False:
        return Reason.STALE_FEED.value
    if check.quality_failed is not False:
        return Reason.QUALITY_FAILED.value
    if check.gate_on is not True:
        return Reason.REGIME_GATE.value
    if check.ltp is None:
        return check.ltp_refusal or PRICE_UNKNOWN
    if signal is None:
        return None
    if signal.stop_price is not None and check.ltp <= Decimal(signal.stop_price):
        return PRICE_AT_OR_BELOW_STOP
    if signal.triggered_at is not None and break_failed(
        Decimal(signal.trigger_price), as_utc(signal.triggered_at), check.closes
    ):
        return BREAK_FAILED
    return None


def _taken(session: Session, signal_id: uuid.UUID) -> bool:
    """The signal's approval card was taken (the Take passed its re-checks: a refused one ended
    the signal Rejected in the same transaction)."""
    approval = session.get(HatApproval, signal_id)
    return approval is not None and approval.decision == approvals.TAKE


def unplaced_entry() -> ColumnElement[bool]:
    """A real entry committed but not placed: an auto signal in Triggered, or a confirm signal
    whose Take passed while it still awaits placement. Needs `HatApproval` outer-joined on the
    signal. Shared by the restart's recovery and the sweep, so they agree (PR 52)."""
    return ((HatSignal.state == lifecycle.TRIGGERED) & (HatSignal.mode == Mode.AUTO)) | (
        (HatSignal.state == lifecycle.AWAITING_APPROVAL) & (HatApproval.decision == approvals.TAKE)
    )


def _kill_refusal(session: Session, bot_id: uuid.UUID) -> str | None:
    """A kill switch or a pause that came on after the risk check (the bot's lock is released
    when the signal commits) stops the entry: the bot is manage-only."""
    kill = kill_ops.kill_state(session, bot_id)
    for tripped, reason in (
        (kill.account_tripped, Reason.KILL_ACCOUNT),
        (kill.bot_tripped, Reason.KILL_BOT),
        (kill.health_tripped, Reason.KILL_HEALTH),
        (kill.paused, Reason.BOT_PAUSED),
    ):
        if tripped:
            return reason.value
    return None


def _gtt_refusal(session: Session, account_id: uuid.UUID, broker: HatBroker) -> str | None:
    """HAT-EXEC-012 at placement: the account must still run real orders (active, GTT for
    delivery in its stored capabilities and in the adapter's now) when a Take arrives minutes
    after its card went out."""
    account = configs_of(session).get(BrokerAccount, account_id)
    if account is None or not real_orders_allowed(
        account.status, account.capabilities or {}, broker.capabilities().gtt_delivery
    ):
        return NO_GTT_DELIVERY
    return None


def _mode_refusal(
    session: Session, signal: HatSignal, now: datetime, calendar: SessionCalendar | None
) -> str | None:
    """HAT-EXEC-012 and HAT-GOV-085 at placement: the bot must still run the mode its entry was
    routed in. A gate that closed after the routing (`revoke-ddpi`, the sleeve leaving Live, the
    weights no longer confirmed) refuses a Take, an auto entry and a resumed one, with the gate's
    code where it is known."""
    bot = configs_of(session).get(Bot, signal.bot_id)
    if bot is None:
        return MODE_GATE_CLOSED
    view = mode_view(session, bot, now, calendar)
    refusal = mode_refusal(signal.mode, view.effective)
    return None if refusal is None else view.why or refusal


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
        issuer: entries.TokenIssuer | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._clock = clock  # stamps each entry's send
        self._sender = OrderSender(broker, guard, clock)
        self._reconciler = reconciler
        self._notifier = notifier or LogNotifier()
        # Signs each approval card's token (the Telegram signer). Without one a confirm card
        # can never be taken (`entries.send_approval`).
        self._issuer = issuer
        # signals whose placement raised after their commit, for the next resume (PR 52)
        self._stranded: set[uuid.UUID] = set()
        self._stranded_lock = threading.Lock()
        # one placement at a time per account: the trigger path, a Take and the periodic resume
        # share this router, and two placements of one signal that both read no open tag would
        # reserve two tags and send two entry orders (PR 52)
        self._placing = threading.RLock()

    # --- routing ------------------------------------------------------------------------------

    def route(
        self,
        session: Session,
        request: entries.EntryRequest,
        *,
        now: datetime,
        check: PlacementCheck,
        decide: entries.DECIDE = evaluate_signal,
    ) -> entries.EntryOutcome:
        """Write the trigger's signal under its effective mode and commit; place an accepted auto
        entry under `check`, the live conditions read by the caller at `now` (the placement
        time, not the trigger's snapshot). `open_entry` reads the signal's confidence label (D132)
        and routes an auto one that is not high as confirm, an approval card for the owner
        (HAT-EXEC-014, D149); placement refuses an auto signal stored without a high label. A
        repeated trigger returns the existing signal (and resumes an auto entry that a crash
        left unplaced)."""
        bot = configs_of(session).get(Bot, request.bot_id)
        if bot is None:
            raise positions.TradeError("unknown_bot")
        mode = bot_mode(session, bot, request.triggered_at, request.context.calendar)
        outcome = entries.open_entry(session, request, decide=decide, mode=mode, issue=self._issuer)
        if outcome.mode != Mode.AUTO or outcome.state != lifecycle.TRIGGERED:
            session.commit()
            return outcome
        self._place_committed(session, outcome.signal_id, now, check)
        signal = session.get(HatSignal, outcome.signal_id)
        state = signal.state if signal is not None else outcome.state
        return entries.EntryOutcome(
            outcome.signal_id,
            state,
            outcome.decision,
            None,
            duplicate=outcome.duplicate,
            mode=outcome.mode,
        )

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
        signal = session.get(HatSignal, signal_id)
        decision = approvals.take(
            session,
            signal_id,
            price=price,
            locked_upper_circuit=locked_upper_circuit,
            now=now,
            chat_id=chat_id,
            refusal=placement_refusal(check, now, signal),
        )
        if decision.place:
            self._place_committed(session, signal_id, now, check)
        else:
            session.commit()
        return decision

    def skip(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> approvals.ApprovalDecision:
        """The user's Skip; the signal continues as a veto trade."""
        decision = approvals.skip(session, signal_id, now=now, chat_id=chat_id)
        session.commit()
        return decision

    def expire(self, session: Session, now: datetime) -> int:
        """The approval sweep: expire what is due and open the missing veto trades, then end
        Rejected (`signal_expired`) every real entry of an earlier trade date left unplaced (PR
        52): its entry order reserved and never sent is cancelled first, and one that may have
        reached the broker is left to reconciliation. Idempotent: a second sweep at the same
        time changes nothing. Returns how many approvals expired."""
        count = approvals.expire_due(session, now)
        session.commit()
        for signal_id in self._stranded_before(session, trade_date_of(now)):
            try:
                self._end_stranded(session, signal_id, now)
            except Exception:  # one signal's failure never stops the others' sweep
                session.rollback()
                logger.exception("ending the stranded entry of signal %s failed", signal_id)
        return count

    def _end_stranded(self, session: Session, signal_id: uuid.UUID, now: datetime) -> None:
        with self._placing:  # never while a resume or a trigger places it
            tag = self._open_entry_tag(session, signal_id)
            if tag is not None and not self._release_unsent(session, tag):
                return  # an entry that may have reached the broker: reconciliation's
            signal = session.get(HatSignal, signal_id)
            if signal is not None:
                session.refresh(signal)
                self._not_placed(session, signal, SIGNAL_EXPIRED, now)

    def take_stranded(self) -> list[uuid.UUID]:
        """The signals whose placement raised since the last call (each returned once)."""
        with self._stranded_lock:
            stranded = sorted(self._stranded)
            self._stranded.clear()
        return stranded

    def remember_stranded(self, signal_id: uuid.UUID) -> None:
        """Keep a signal whose resumed placement raised for the next resume."""
        with self._stranded_lock:
            self._stranded.add(signal_id)

    def _place_committed(
        self, session: Session, signal_id: uuid.UUID, now: datetime, check: PlacementCheck
    ) -> None:
        """Commit the signal (or the Take) and place its entry; one whose commit or placement
        raises is remembered for the next resume (`take_stranded`: a commit that did not land
        leaves nothing to place there) and the error goes on to the caller."""
        try:
            session.commit()
            self.place_entry(session, signal_id, now=now, check=check)
        except Exception:
            self.remember_stranded(signal_id)
            raise

    @staticmethod
    def _stranded_before(session: Session, day: date) -> list[uuid.UUID]:
        """Real entries of a trade date before `day` still waiting to be placed: an auto signal
        in Triggered, or a Take not yet placed."""
        return list(
            session.scalars(
                select(HatSignal.id)
                .outerjoin(HatApproval, HatApproval.signal_id == HatSignal.id)
                .where(HatSignal.trade_date < day, unplaced_entry())
                .order_by(HatSignal.id)
            )
        )

    # --- placing a real entry -------------------------------------------------------------------

    def place_entry(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, check: PlacementCheck
    ) -> SendOutcome | None:
        """Place the real entry of an accepted auto signal or a passed Take, at the entry price
        the engine fixed at the trigger, when `check` allows an entry at `now`, no kill switch or
        pause is on and the account still runs real orders. Safe to call again: an order already
        reserved is sent (or looked up) under its own tag, and a signal that left Triggered or
        AwaitingApproval, or a card not yet taken, is left alone. On a refusal a reserved order
        that never reached the broker is cancelled and one that may have is left to
        reconciliation; nothing is sent. Returns the last send, or None when nothing was sent.
        Placements through this router run one at a time, each reading the signal afresh."""
        with self._placing:
            return self._place_entry(session, signal_id, now=now, check=check)

    def _place_entry(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, check: PlacementCheck
    ) -> SendOutcome | None:
        signal = session.get(HatSignal, signal_id)
        if signal is None:
            raise positions.TradeError("unknown_signal")
        session.refresh(signal)
        if signal.state not in _ROUTABLE or signal.mode == Mode.SHADOW or signal.qty <= 0:
            return None
        if signal.state == lifecycle.AWAITING_APPROVAL and not _taken(session, signal.id):
            return None  # a card is placed only after its Take passed (PR 52)
        bot = configs_of(session).get(Bot, signal.bot_id)
        if bot is None:
            raise positions.TradeError("unknown_bot")
        account_id = bot.broker_account_id
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == signal.instrument_id)
        )
        if symbol is None:
            return self._not_placed(session, signal, "unknown_instrument", now)
        refusal, now = self._preflight(session, signal, account_id, now, check)
        if refusal is not None:
            tag = self._open_entry_tag(session, signal.id)
            if tag is not None and not self._release_unsent(session, tag):
                logger.warning(
                    "entry %s refused (%s) but may be placed; left to reconcile", tag, refusal
                )
                return None
            return self._not_placed(session, signal, refusal, now)
        return self._send_entry(session, signal, account_id, now)

    def _preflight(
        self,
        session: Session,
        signal: HatSignal,
        account_id: uuid.UUID,
        now: datetime,
        check: PlacementCheck,
    ) -> tuple[str | None, datetime]:
        """Why the entry may not be placed (a fixed code), or None, and the instant it was
        decided at. The live conditions are checked, the trade date's first entry waits for a
        successful reconciliation (HAT-BRK-110), and then, right before the send, they are
        checked again at a fresh instant with the check read again (`PlacementCheck.reread`): a
        reconciliation's broker round trip must not send on a price read before it. This only
        adds refusals."""
        if signal.trade_date != trade_date_of(now):
            return SIGNAL_EXPIRED, now  # its price, size and risk decision were for that session
        if signal.mode == Mode.AUTO and signal.confidence != HIGH:
            return NOT_HIGH_CONFIDENCE, now  # auto places only a high label (HAT-EXEC-014, D149)
        started = monotonic()
        refusal = self._refusal(session, signal, account_id, now, check)
        if refusal is not None:
            return refusal, now
        if not self._guard.reconciled_today():
            try:
                self._reconciler.run(session, account_id)
            except BrokerUnavailableError:
                return "broker_unavailable", now
        now += timedelta(seconds=monotonic() - started)
        if check.reread is not None:
            check = check.reread(now)
        return self._refusal(session, signal, account_id, now, check), now

    def _refusal(
        self,
        session: Session,
        signal: HatSignal,
        account_id: uuid.UUID,
        now: datetime,
        check: PlacementCheck,
    ) -> str | None:
        return (
            placement_refusal(check, now, signal)
            or _kill_refusal(session, signal.bot_id)
            or _mode_refusal(session, signal, now, check.calendar)
            or _gtt_refusal(session, account_id, self._broker)
            or _session_refusal(session, account_id, now)
        )

    def _send_entry(
        self,
        session: Session,
        signal: HatSignal,
        account_id: uuid.UUID,
        now: datetime,
    ) -> SendOutcome | None:
        """Send the entry under its open tag or a new one; a rejected entry is retried once."""
        sent: SendOutcome | None = None
        tag = self._open_entry_tag(session, signal.id)
        while True:
            if tag is None:
                if self._entry_rejections(session, signal.id) >= MAX_ENTRY_REJECTIONS:
                    # HAT-EXEC-023 across a restart (audit T-F4): a crash after the second
                    # rejection, before the signal ended, never sends a third order
                    return self._not_placed(session, signal, "order_rejected", now)
                refusal = self._guard.refusal(signal.bot_id, account_id, is_entry=True)
                if refusal is not None:
                    return self._not_placed(session, signal, refusal, now)
                tag = reserve_order(session, self._entry_request(session, signal, account_id))
            done, sent = self._send_once(session, signal, tag, now, previous=sent)
            if done:
                return sent
            tag = None  # the one retry: a new logical order, so the next tag

    def _send_once(
        self,
        session: Session,
        signal: HatSignal,
        tag: str,
        now: datetime,
        *,
        previous: SendOutcome | None,
    ) -> tuple[bool, SendOutcome | None]:
        """One send of `tag`: (finished, the outcome to return). Not finished only after a
        rejection that may still be retried."""
        try:
            sent = send_order(session, self._sender, tag)
        except (OrderRefusedError, BrokerUnavailableError) as exc:
            code = exc.code if isinstance(exc, OrderRefusedError) else "broker_unavailable"
            if self._release_unsent(session, tag):
                return True, self._not_placed(session, signal, code, now)
            logger.warning("entry %s outcome unknown after %s; left to reconcile", tag, code)
            return True, previous
        except UnresolvedOrderError:
            logger.warning("entry %s of unknown outcome not sent again; left to the fill sync", tag)
            self._alert(ALERT_ENTRY_UNRESOLVED, signal, {"tag": tag})
            return True, previous
        if sent.status != "rejected":
            return True, sent
        self._alert(ALERT_ORDER_REJECTED, signal, {"tag": tag, "code": sent.code})
        if self._entry_rejections(session, signal.id) >= MAX_ENTRY_REJECTIONS:
            self._not_placed(session, signal, "order_rejected", now)
            return True, sent
        return False, sent

    def _entry_request(
        self, session: Session, signal: HatSignal, account_id: uuid.UUID
    ) -> OrderRequest:
        price = snapshot.entry_price_of(session, signal)
        return OrderRequest(
            bot_id=signal.bot_id,
            signal_id=signal.id,
            broker_account_id=account_id,
            purpose=ENTRY,
            side=Side.BUY,
            qty=signal.qty,
            order_type=OrderType.LIMIT,
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
        if entries.reject_signal(session, signal.id, reason, now):
            session.commit()
            self._alert(ALERT_ENTRY_NOT_PLACED, signal, {"reason": reason})
        else:
            session.rollback()

    def _alert(self, code: str, signal: HatSignal, detail: dict[str, str]) -> None:
        day: date = signal.trade_date or trade_date_of(signal.triggered_at)
        self._notifier.alert(
            code, day, {"bot_id": str(signal.bot_id), "signal_id": str(signal.id), **detail}
        )
