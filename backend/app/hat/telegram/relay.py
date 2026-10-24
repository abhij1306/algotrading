"""The event relay: what the event log says, sent to the chat (HAT-TG-023, HAT-TG-024,
HAT-DEC-T15, HAT-EXEC-043; the model is `verification/tla/ExecutionRouter.tla`, RejectNoCard).

The relay reads `hat_event` in id order from its cursor (`hat_telegram_cursor` `events`) and
sends at most one message per event:

- `signal.triggered` of a confirm signal with an approval: the approval card, Take and Skip
  buttons signed for that approval (and the card's message id on `hat_approval`); of a shadow
  signal with its virtual position: the shadow-trade message. A rejected signal has neither, so
  it reaches the chat only in the 15:35 rejection digest (HAT-EXEC-043).
- `order.filled`, `exit.partial`, `position.closed` and `stop.moved` of a real order or
  position: price, qty and R so far.
- `quality.failed`, `feed.stale`, `broker.session_lost`, `broker.capabilities_changed` (the
  adapter's GTT for delivery reached the account, HAT-BRK-092), `corporate_action.unhandled` (a
  position spans an action the rescale cannot handle, HAT-EXEC-144; URGENT),
  `corporate_action.demerger_exit` and `corporate_action.demerger_late` (a position held into a
  demerger: sell it by a close, or it was held into one, CA8; URGENT), `order.rejected`
  and `relay.skipped`: a health alert (T15). The `Notifier` leaves these codes to the relay, so
  each failure is one alert.

An event is relayed only once it is `RELAY_LAG` old, so a transaction that took a lower id and
committed a little later is still seen in order. An id the cursor passes without seeing it (a
rolled-back transaction, or one still open) is kept as a gap and looked up again on every run
for `GAP_WAIT`, so a transaction that commits late is relayed late rather than lost. The gaps are
held in memory: a restart forgets them. The cursor moves after the message is sent, in the same
commit as its `hat_telegram_message` row: a crash in between resends one message rather than
losing it. A send that fails stops the run with the cursor before that event, so a passing
outage loses nothing; a refusal that will not pass (a 400, 403 or 404) is tried `MAX_REFUSALS`
times, then the event is skipped with a `relay.skipped` event (an alert naming the event and its
subject, sent once Telegram takes messages again; a skipped card's approval expires unanswered
and the signal goes on as a veto trade). An event that
cannot be rendered is logged by id and skipped, so it does not hold back the ones after it. On
its first run the relay starts at the newest event: history is not replayed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..clock import SYSTEM_CLOCK, Clock
from ..engine.confidence import stored_basis
from ..events import record
from ..governance import Mode
from ..models import (
    HatAnnouncement,
    HatApproval,
    HatEvent,
    HatOrder,
    HatPosition,
    HatSignal,
    HatSignalAnnouncement,
    HatTelegramCursor,
    HatTradeResult,
)
from ..money import exact
from ..read.positions import symbol_of
from ..time import as_utc, trade_date_of, utc_now
from . import reads, render, store
from .client import BotClient, Button, TelegramError
from .tokens import SKIP, TAKE, ApprovalSigner

__all__ = [
    "HEALTH_EVENTS",
    "POSITION_EVENTS",
    "RELAY_LAG",
    "EventRelay",
    "Outbound",
    "message_for",
]

logger = logging.getLogger(__name__)

RELAY_LAG = timedelta(seconds=5)
SKIPPED = "relay.skipped"
# A refusal that will not pass (`TelegramError.retryable` False) is tried this many times, then
# its event is skipped, so it cannot hold back the cards and alerts after it.
MAX_REFUSALS = 3
POSITION_EVENTS = frozenset({"order.filled", "exit.partial", "position.closed", "stop.moved"})
HEALTH_EVENTS = frozenset(
    {
        "quality.failed",
        "feed.stale",
        "broker.session_lost",
        "broker.capabilities_changed",
        "corporate_action.unhandled",
        "corporate_action.demerger_exit",
        "corporate_action.demerger_late",
        "order.rejected",
        SKIPPED,
    }
)
TRIGGERED = "signal.triggered"
_BATCH = 100
GAP_WAIT = timedelta(hours=1)
_MAX_GAPS = 10_000


@dataclass(frozen=True)
class Outbound:
    """One message to send for an event: its kind, subject, text and buttons."""

    kind: str
    ref_id: object
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()


def card_view(session: Session, signal: HatSignal, approval: HatApproval | None) -> render.CardView:
    """The card of a signal exactly as the engine fixed it (HAT-TG-023)."""
    payload = session.scalar(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal.id, HatEvent.event_type == TRIGGERED
        )
    )
    pct = exact((payload or {}).get("max_chase_pct"), floats=True)
    trigger = Decimal(signal.trigger_price)
    news = session.execute(
        select(HatAnnouncement.title, HatAnnouncement.url)
        .join(HatSignalAnnouncement, HatSignalAnnouncement.announcement_id == HatAnnouncement.id)
        .where(HatSignalAnnouncement.signal_id == signal.id)
        .order_by(HatAnnouncement.published_at.desc())
        .limit(1)
    ).first()
    return render.CardView(
        signal_id=signal.id,
        symbol=symbol_of(session, signal.instrument_id),
        playbook=signal.playbook,
        entry=trigger,
        stop=Decimal(signal.stop_price),
        qty=signal.qty,
        risk=Decimal(signal.risk_amount),
        max_chase=trigger * (1 + pct / 100) if pct is not None else None,
        expires_at=as_utc(approval.expires_at) if approval is not None else None,
        headline=news[0] if news else None,
        pdf_url=news[1] if news else None,
        confidence=signal.confidence,
        basis=stored_basis(signal.confidence_basis),
    )


def _signal_message(session: Session, event: HatEvent, signer: ApprovalSigner) -> Outbound | None:
    signal = session.get(HatSignal, event.aggregate_id)
    if signal is None or signal.qty <= 0:
        return None
    approval = session.get(HatApproval, signal.id)
    if signal.mode == Mode.CONFIRM and approval is not None and approval.decision is None:
        view = card_view(session, signal, approval)
        buttons = (
            (
                ("Take", signer.button(TAKE, signal.id, approval.expires_at)),
                ("Skip", signer.button(SKIP, signal.id, approval.expires_at)),
            ),
        )
        return Outbound("card", signal.id, render.card_text(view), buttons)
    if signal.mode == Mode.SHADOW:
        virtual = session.scalar(
            select(HatPosition.id).where(
                HatPosition.signal_id == signal.id, HatPosition.is_virtual.is_(True)
            )
        )
        if virtual is not None:
            return Outbound(
                "shadow", signal.id, render.shadow_text(card_view(session, signal, None))
            )
    return None


def _position_of(session: Session, event: HatEvent) -> HatPosition | None:
    if event.aggregate_type == "position":
        return session.get(HatPosition, event.aggregate_id)
    if event.aggregate_type == "order":
        order = session.get(HatOrder, event.aggregate_id)
        if order is None or order.is_virtual:
            return None
        if order.position_id is not None:
            return session.get(HatPosition, order.position_id)
        return session.scalars(
            select(HatPosition).where(HatPosition.signal_id == order.signal_id)
        ).first()
    return None


def _position_message(session: Session, event: HatEvent) -> Outbound | None:
    position = _position_of(session, event)
    if position is None or position.is_virtual:
        return None
    payload = event.payload or {}
    price = exact(
        payload.get("price") if event.event_type != "stop.moved" else payload.get("to"), floats=True
    )
    qty = payload.get("qty")
    dividends = None
    if event.event_type == "position.closed":
        result = session.get(HatTradeResult, position.id)
        r = Decimal(result.r_multiple) if result is not None else None
        dividends = Decimal(result.dividends) if result is not None else None
    else:
        r = reads.r_so_far(Decimal(position.avg_entry), Decimal(position.initial_stop), price)
    view = render.PositionEventView(
        symbol=symbol_of(session, position.instrument_id),
        qty=qty if isinstance(qty, int) and not isinstance(qty, bool) else None,
        price=price,
        r_so_far=r,
        dividends=dividends,
    )
    return Outbound(
        event.event_type, position.id, render.position_event_text(event.event_type, view)
    )


def _health_message(event: HatEvent) -> Outbound:
    detail = {
        k: v
        for k, v in (event.payload or {}).items()
        if isinstance(v, str | int) and not isinstance(v, bool)
    }
    # a list of fixed check names (the quality gate's `failed`): one line each, up to ten
    for key, value in (event.payload or {}).items():
        if isinstance(value, list):
            names = [x for x in value if isinstance(x, str)][:10]
            detail.update({f"{key}.{n}": name for n, name in enumerate(names, start=1)})
    text = render.alert_text(event.event_type, trade_date_of(as_utc(event.occurred_at)), detail)
    return Outbound(f"alert:{event.event_type}", event.bot_id, text)


def message_for(session: Session, event: HatEvent, signer: ApprovalSigner) -> Outbound | None:
    """The one message an event produces, or None."""
    if event.event_type == TRIGGERED:
        return _signal_message(session, event, signer)
    if event.event_type in HEALTH_EVENTS:
        return _health_message(event)
    if event.event_type in POSITION_EVENTS:
        return _position_message(session, event)
    return None


class EventRelay:
    def __init__(
        self,
        client: BotClient,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        signer: ApprovalSigner,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._client = client
        self._factory = session_factory
        self._signer = signer
        self._clock = clock
        self._gaps: dict[int, datetime] = {}  # unseen event id -> when it was first passed
        self._refusals: dict[int, int] = {}  # event id -> permanent refusals so far
        self._sent = 0

    def run_once(self) -> int:
        """Relay the settled events after the cursor; return how many messages were sent."""
        now = utc_now(self._clock)
        sent = 0
        with self._factory() as session:
            if session.get(HatTelegramCursor, store.EVENTS) is None:
                newest = session.scalar(select(func.coalesce(func.max(HatEvent.id), 0)))
                store.set_cursor(session, store.EVENTS, int(newest or 0), now)
                session.commit()
                return 0
            last = store.cursor(session, store.EVENTS)
            settled = as_utc(now) - RELAY_LAG
            late = self._late_events(session, settled, now)
            events = session.scalars(
                select(HatEvent)
                .where(HatEvent.id > last, HatEvent.recorded_at <= settled)
                .order_by(HatEvent.id)
                .limit(_BATCH)
            ).all()
            for event in late:
                if not self._relay(session, event, now):
                    return sent
                self._gaps.pop(event.id, None)
                session.commit()
                sent += self._sent
            for event in events:
                if not self._relay(session, event, now):
                    return sent
                store.set_cursor(session, store.EVENTS, event.id, now)
                session.commit()
                sent += self._sent
                self._note_gaps(last, event.id, now)  # only ids the cursor has now passed
                last = event.id
        return sent

    def _note_gaps(self, last: int, event_id: int, now: datetime) -> None:
        for missing in range(last + 1, event_id):
            if len(self._gaps) >= _MAX_GAPS:
                break
            self._gaps.setdefault(missing, now)

    def _late_events(self, session: Session, settled: datetime, now: datetime) -> list[HatEvent]:
        """The events of remembered gaps that have committed since; gaps older than `GAP_WAIT`
        are dropped (a rolled-back transaction never fills its id)."""
        for gap, seen in list(self._gaps.items()):
            if now - seen > GAP_WAIT:
                del self._gaps[gap]
        if not self._gaps:
            return []
        return list(
            session.scalars(
                select(HatEvent)
                .where(HatEvent.id.in_(list(self._gaps)), HatEvent.recorded_at <= settled)
                .order_by(HatEvent.id)
            )
        )

    def _relay(self, session: Session, event: HatEvent, now: datetime) -> bool:
        """Send the message of one event (none for most); False when the send failed and the
        run must stop. `_sent` is 1 when a message went out."""
        self._sent = 0
        try:
            out = message_for(session, event, self._signer)
        except Exception as exc:  # one unrenderable event must not hold back the rest
            session.rollback()
            logger.error("telegram relay skipped event %s: %s", event.id, type(exc).__name__)
            return True
        if out is None:
            return True
        try:
            message_id = self._client.send(out.text, out.buttons)
        except TelegramError as exc:
            event_id, event_type, subject = event.id, event.event_type, event.aggregate_id
            session.rollback()
            if exc.retryable or not self._refused_enough(event_id):
                logger.warning("telegram relay stopped at event %s: %s", event_id, exc.code)
                return False
            logger.error(
                "telegram relay skipped event %s (%s) after %d refusals: %s",
                event_id,
                event_type,
                MAX_REFUSALS,
                exc.code,
            )
            if event_type != SKIPPED:  # the alert of a skip is never itself alerted
                _record_skip(session, event_id, event_type, subject, exc.code, now)
            return True
        self._refusals.pop(event.id, None)
        store.record_message(
            session,
            chat_id=self._client.chat_id,
            kind=out.kind,
            ref_id=out.ref_id,
            message_id=message_id,
            sent_at=now,
        )
        if out.kind == "card":
            approval = session.get(HatApproval, out.ref_id)
            if approval is not None:
                approval.telegram_message_id = message_id
        self._sent = 1
        return True

    def _refused_enough(self, event_id: int) -> bool:
        """Count one permanent refusal of the event; True on the last one allowed."""
        count = self._refusals.get(event_id, 0) + 1
        if count < MAX_REFUSALS:
            self._refusals[event_id] = count
            return False
        self._refusals.pop(event_id, None)
        return True


def _record_skip(
    session: Session,
    event_id: int,
    event_type: str,
    subject: uuid.UUID,
    code: str,
    now: datetime,
) -> None:
    """The trail of a skipped event, in the transaction that moves the cursor past it; the
    relay sends it as an alert once Telegram takes messages again. `subject` is the skipped
    event's aggregate (a skipped card's signal, on the dashboard's signals list)."""
    record(
        session,
        aggregate_type="session",
        aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hat:relay:{event_id}"),
        event_type=SKIPPED,
        actor="engine",
        payload={
            "event_id": event_id,
            "event_type": event_type,
            "subject": str(subject),
            "code": code,
        },
        occurred_at=now,
    )
