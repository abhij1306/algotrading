"""Live trade management of real positions (HAT-EXEC-060..064, -073, -074,
HAT-OPS-023, -024, -032, -034; HAT-DEC-B17, B18; PR 15c).

`TradeManager` serves one broker account's session, beside the `ExecutionRouter` that places its
entries. It runs the exit stack on real positions and the jobs around the close:

- **Live price events** (`on_price`): the same `exits.evaluate` as shadow positions (T12) via
  `live_trades.evaluate_real_position`; a raised stop is followed at the broker
  (`StopDesk.follow`), and a sale is sent at once as a MARKET SELL CNC. Exits never wait for the
  user and never check a kill switch or pause. `hard_stop` is the broker's: while a stop
  rests at the broker the engine sends no sale of its own for it (two sales would oversell); a
  position without a working broker stop is sold by the engine.
- **`exits.close`** (`close_exits`, 15:20-15:25 IST, HAT-EXEC-074): every open position of the
  account's bots, virtual and real, is evaluated on the live price in the close phase, and each
  `close_below_sma` evaluation is recorded with its live price (`exit.close_evaluated`,
  HAT-OPS-034). The body refuses to run outside the window and sends no sale after 15:30.
- **`gtt.swap`** (`swap_stops`, ~15:25) and **`gtt.verify`** (`verify_stops`, nightly): through
  the `StopDesk`; the verify first reads the broker's fills so a position the GTT sold is closed.
- **`gtt.check`** (`check_gtts`, 08:40, HAT-EXEC-145, P1): the day's first read of the broker's
  GTT book, by the fill sync's own GTT check (`FillSync` step 6c, every few minutes after): a GTT
  the broker cancelled (an ex-date, a series change, the owner in the Fyers app) is ended and the
  position protected again. A position whose instrument changed series is alerted URGENT
  (`position.series_changed`, HAT-EXEC-146; also by every nightly verify) and its GTT under the
  old series' symbol replaced.
- **Session end** (`end_session`): an entry whose order the broker's book does not hold after a
  successful read (its outcome was unknown) ends its signal Rejected with `entry_not_found`; a
  DAY limit cannot fill after the close. (An entry first found already ended is settled when it
  is adopted: `submit.adopt_broker_order`.) A sale of unknown outcome the book does not hold
  never landed: it ends `expired` and its rules fire again.

Every sale (an exit, a flatten, a market price protection re-send) is one `SaleOrder`, and one
pure `sale_gate` decides whether and how it goes. A sale is retried once after a rejection with
the next tag, and at most two rejected sales of one purpose are sent per signal (HAT-OPS-032);
then (PR 48) one LIMIT sale at the day's lower circuit price is sent instead (the lowest price
that can trade today, so no price check refuses it) and the owner is told (`sale.unfilled`, code
`band_limit`); without a known band, or once that sale is refused too, `exit.not_placed` is
raised and the broker stop keeps protecting the position. A rejection the broker sends after
taking the sale counts the same (`FillSync`, HAT-EXEC-075). A sale whose answer was lost is
looked up by its tag before the position is sold again (HAT-EXEC-065): found, it is adopted;
absent after a good read, it ends `expired` and its rules fire again, so it never blocks the
position's later sales. No sale, stop or fill sync waits for a reconciliation: a
failed one blocks new entries only (D118).

Market price protection (PR 48, behind `HAT_MPP_RESEND`) is `mpp.MarketProtection`: after each
fill sync it cancels a market sale resting unfilled and sends the shares it left again through
`sell_again`, the exit path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..engine import close_gap, entries, exits, live_trades, shadow, snapshot
from ..engine.botlock import lock_bot
from ..engine.rescale import unhandled_actions
from ..events import record
from ..models import (
    HatBot,
    HatInstrument,
    HatOrder,
    HatPosition,
    HatSignal,
)
from ..notify import LogNotifier, Notifier
from ..risk.sessions import SessionCalendar, SessionState
from ..time import IST, as_utc, trade_date_of
from .broker import BrokerOrder, BrokerUnavailableError, HatBroker, OrderType, Side
from .fills import ALERT_SALE_UNFILLED, FillSync, SaleBand, SyncReport
from .gtt_upkeep import GttUpkeep
from .guard import ExecutionGuard
from .order_book import (
    EXIT_SALES,
    MAX_SALE_REJECTIONS,
    adopt_broker_order,
    end_lost_order,
    rejected_sales,
)
from .position_desk import Verdict
from .protect import (
    ALERT_EXIT_NOT_PLACED,
    CLOSED,
    FAILED,
    REPLACED,
    SWAPPED,
    UNVERIFIED,
    VERIFIED,
    StopDesk,
)
from .reconcile import baseline_at, broker_quantities
from .series import ALERT_SERIES_CHANGED, broker_symbol, series_changed
from .stop_plans import LIVE, UNSURE
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
    "ALERT_EXIT_NOT_PLACED",
    "ALERT_SALE_REJECTED",
    "EXITS_CLOSE_FROM",
    "EXITS_CLOSE_UNTIL",
    "ORDERS_UNTIL",
    "FLATTEN",
    "CloseReport",
    "FlattenReport",
    "ManagerConfig",
    "SaleGate",
    "SaleOrder",
    "SaleStep",
    "StopsReport",
    "TradeManager",
    "ViewSource",
    "earlier_sale_verdict",
    "in_close_window",
    "sale_gate",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
EXIT_FULL = "exit_full"
EXIT_PARTIAL = "exit_partial"
# HAT-EXEC-074, HAT-DEC-B18: the close-phase rules run between 15:20 and 15:25 IST on the live
# price, and their sales go out before the 15:30 close.
EXITS_CLOSE_FROM = time(15, 20)
EXITS_CLOSE_UNTIL = time(15, 25)
ORDERS_UNTIL = time(15, 30)
ALERT_SALE_REJECTED = "order.rejected"
ENTRY_NOT_FOUND = "entry_not_found"
BAND_LIMIT = "band_limit"  # alert code: a sale refused twice sent as a limit at the lower band
FLATTEN = "flatten"
# What settling a position's sales of unknown outcome found (`_settle_sales`).
SETTLED = "settled"
WAIT = "wait"
UNREADABLE = "unreadable"
# What the broker's holdings say of a lost sale of an earlier session (`earlier_sale_verdict`).
NEVER_SOLD = "never_sold"
SOLD = "sold"
UNKNOWN = "unknown"
ALERT_SALE_SETTLED = "sale.settled_from_holdings"
EVENT_QTY_CORRECTED = "position.qty_corrected"

# The live view of a position at the close (price, SMA levels, days held); None when unknown.
ViewSource = Callable[[Session, HatPosition], exits.ExitView | None]


class _RunClock:
    """A run's clock: the caller's `start` plus the monotonic seconds the run has taken."""

    def __init__(self, start: datetime, ticker: Callable[[], float]) -> None:
        self._start = start
        self._ticker = ticker
        self._began = ticker()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._ticker() - self._began)


def earlier_sale_verdict(qty_open: int, broker_held: int, row_qty: int) -> str:
    """What the shares the broker holds for a position (`broker_held`) say of its lost sales of
    an earlier session (`row_qty` shares in all), when HAT counts `qty_open` open: NEVER_SOLD
    when every open share is still held, SOLD when the shares gone cover the sales, UNKNOWN
    otherwise (part sold, or held shares that the sales do not explain)."""
    if broker_held >= qty_open:
        return NEVER_SOLD
    if qty_open - broker_held >= row_qty:
        return SOLD
    return UNKNOWN


def in_close_window(now: datetime) -> bool:
    """15:20 <= now < 15:25 IST."""
    local = now.astimezone(IST).time()
    return EXITS_CLOSE_FROM <= local < EXITS_CLOSE_UNTIL


@dataclass(frozen=True)
class SaleOrder:
    """One sale of a real position: `qty` shares under `purpose` (an exit, a flatten) for the
    exit rules `rule_ids` (none for a flatten). `rule` is the rule its events name: the first
    rule's name, else the purpose."""

    position: HatPosition
    purpose: str
    qty: int
    rule_ids: tuple[str, ...] = ()

    @property
    def rule(self) -> str:
        return next((rid.partition(":")[2] for rid in self.rule_ids), "") or self.purpose


class SaleStep(StrEnum):
    HOLD = "hold"  # nothing now, nothing told: another sale works, a settle waits, budget spent
    REFUSE = "refuse"  # nothing now, told (`exit.not_placed` with the gate's code)
    SEND = "send"  # make room, then the market sale `attempts` times, then the band limit


@dataclass(frozen=True)
class SaleGate:
    step: SaleStep
    code: str | None = None  # REFUSE: the `exit.not_placed` reason
    attempts: int = 0  # SEND: market sales left in the session's rejection budget
    floor: Decimal | None = None  # SEND: the lower band a sale refused twice goes at once


def sale_gate(
    settled: str,
    qty: int,
    *,
    busy: bool,
    open_now: bool,
    rejected: int,
    floor: Decimal | None,
) -> SaleGate:
    """Whether a sale of `qty` shares goes out, and how: the one place the sale rules are decided
    (HAT-OPS-032, HAT-EXEC-065, -066, -074, PR 48). `settled`: what settling the position's lost
    sales found (`SETTLED`, `WAIT`, `UNREADABLE`); `busy`: another sale of it works at the
    broker (a triggered GTT's sale is one); `open_now`: the session takes orders at the moment of
    sending; `rejected`: the purpose's sales the broker refused this session; `floor`: the day's
    lower circuit price, None when unknown.

    The market sale goes `MAX_SALE_REJECTIONS - rejected` times; refused that often, once more as
    a limit at `floor`. A budget spent before this sale was told then: HOLD."""
    if settled == UNREADABLE:
        return SaleGate(SaleStep.REFUSE, code="sale_unresolved")
    if qty <= 0 or settled == WAIT or busy:
        return SaleGate(SaleStep.HOLD)  # one sale at a time; the next evaluation sees what it left
    if not open_now:
        return SaleGate(SaleStep.REFUSE, code="market_closed")
    if rejected > MAX_SALE_REJECTIONS or (rejected == MAX_SALE_REJECTIONS and floor is None):
        return SaleGate(SaleStep.HOLD)
    return SaleGate(SaleStep.SEND, attempts=MAX_SALE_REJECTIONS - rejected, floor=floor)


@dataclass(frozen=True)
class ManagerConfig:
    """What a `TradeManager` is built with beside its broker, guard, stop desk and fill sync: the
    account's one `GttUpkeep` (its `FillSync` holds the same), the notifier, the day's lower band
    (for a sale refused twice, PR 48), and the time a sale is sent at: `sending` when given, else
    the caller's `now` plus the time the run has taken so far (`ticker`, monotonic seconds)."""

    upkeep: GttUpkeep
    notifier: Notifier = field(default_factory=LogNotifier)
    band: SaleBand | None = None
    sending: Clock | None = None
    ticker: Callable[[], float] = monotonic


@dataclass(frozen=True)
class _Sale:
    """A sale under the position lock: the order, its signal and its market request."""

    order: SaleOrder
    signal: HatSignal
    request: OrderRequest


@dataclass
class CloseReport:
    evaluated: int = 0
    sales: int = 0
    recorded: int = 0
    failed: int = 0
    refused: str | None = None


@dataclass
class FlattenReport:
    """What a flatten did: the open real positions it found, the sales it sent and the
    positions whose sale failed with an error (each logged; the others are still sold)."""

    positions: int = 0
    sent: int = 0
    failed: int = 0


@dataclass
class StopsReport:
    done: int = 0
    failed: list[uuid.UUID] = field(default_factory=list)
    unread: bool = False  # the fills or the GTT book could not be read (`check_gtts`)


class TradeManager:
    """Exits and broker stops of one broker account's real positions (and the account's virtual
    ones at the close)."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        desk: StopDesk,
        fills: FillSync,
        config: ManagerConfig,
        *,
        calendar: SessionCalendar | None,
    ) -> None:
        self._broker = broker
        self._band = config.band
        self._guard = guard
        self._desk = desk
        self._upkeep = config.upkeep  # the swap, nightly verify and GTT-book check
        # The position locks and the settle wait the stop desk uses; its clock stamps the sends.
        self._positions = desk.positions
        self._clock = desk.positions.clock
        self._sender = OrderSender(broker, guard, self._clock)
        self._fills = fills
        self._mpp = fills.mpp  # market price protection (PR 48), shared with the fill sync
        self._calendar = calendar
        self._notifier = config.notifier
        self._sending = config.sending
        self._ticker = config.ticker
        # positions whose lost earlier sales wait for the owner (no holding baseline): told once
        self._baseline_alerted: set[uuid.UUID] = set()

    def use_calendar(self, calendar: SessionCalendar | None) -> None:
        """The trading calendar read again (a refresh, a new year's rows), for the manager and its
        stop desk."""
        self._calendar = calendar
        self._desk.use_calendar(calendar)

    # --- reading ------------------------------------------------------------------------------

    @staticmethod
    def _bots(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            session.scalars(select(HatBot.id).where(HatBot.broker_account_id == account_id))
        )

    def open_positions(
        self, session: Session, account_id: uuid.UUID, *, real_only: bool = True
    ) -> list[HatPosition]:
        """The account's open positions (real only, or with the virtual ones), oldest first."""
        query = select(HatPosition).where(
            HatPosition.bot_id.in_(self._bots(session, account_id)),
            HatPosition.closed_at.is_(None),
            HatPosition.qty_open > 0,
        )
        if real_only:
            query = query.where(HatPosition.is_virtual.is_(False))
        return list(session.scalars(query.order_by(HatPosition.opened_at, HatPosition.id)))

    def orders_allowed(self, now: datetime) -> bool:
        """A sale goes out only inside the calendar session and before 15:30 IST."""
        return self._orders_allowed(now)

    def _orders_allowed(self, now: datetime) -> bool:
        open_now = self._calendar is not None and self._calendar.state(now) is SessionState.OPEN
        return open_now and now.astimezone(IST).time() < ORDERS_UNTIL

    # --- live price events --------------------------------------------------------------------

    def on_price(
        self, session: Session, position_id: uuid.UUID, view: exits.ExitView, now: datetime
    ) -> exits.Evaluation:
        """Run the live rules of one real position on a price event and carry them out."""
        return self._evaluate(session, position_id, view, "live", now, self._sending_clock(now))

    def _sending_clock(self, now: datetime) -> Clock:
        """The clock a run reads at each sale: a close run that started at 15:24 and is still
        sending at 15:30 sends nothing more (HAT-EXEC-074)."""
        if self._sending is not None:
            return self._sending
        return _RunClock(now, self._ticker)

    def _evaluate(
        self,
        session: Session,
        position_id: uuid.UUID,
        view: exits.ExitView,
        phase: exits.Phase,
        now: datetime,
        sending: Clock,
    ) -> exits.Evaluation:
        result = live_trades.evaluate_real_position(session, position_id, view, phase, now)
        session.commit()
        if result.new_stop is not None:
            self._desk.follow(session, position_id, now)
        for action in result.actions:
            self._sell(session, position_id, action, now, sending)
        return result

    def cancel_unfilled_entry(
        self, session: Session, signal_id: uuid.UUID, now: datetime, reason: str
    ) -> bool:
        """Cancel the signal's real entry while nothing of it filled (PR 49, HAT-EXEC-046):
        `EntryCancel.cancel_unfilled`, for `reason`. A cancel is never an entry, so no gate holds
        it."""
        return self._desk.entries.cancel_unfilled(session, signal_id, now, reason)

    # --- flatten (HAT-TG-035, HAT-GOV-082) ----------------------------------------------------

    def flatten(self, session: Session, account_id: uuid.UUID, now: datetime) -> FlattenReport:
        """Sell every open real position of the account at market (`purpose = flatten`, side
        sell, the position's whole `qty_open`), through the exit path: the same session,
        reconciliation and stop checks as any exit, never a kill or pause check. It can only
        reduce exposure. A position already being sold is left to that sale. Virtual positions
        are measurements, not exposure, and are left alone. The confirmation and the
        `flatten.executed` event are the caller's (the Telegram edge).

        A position spanning a corporate action the rescale cannot handle (HAT-EXEC-144) holds a
        quantity of the old scale, so its sale is capped at the shares the broker holds of the
        symbol (a consolidation leaves fewer), less what the flatten already sized for other
        positions of that symbol; an unreadable holdings book leaves the cap off, since the
        broker refuses an oversell anyway and a flatten is never blocked."""
        report = FlattenReport()
        sending = self._sending_clock(now)
        held: list[dict[str, int] | None] = []  # read once, spent as capped sales are sized
        for position in self.open_positions(session, account_id):
            report.positions += 1
            try:
                qty = position.qty_open
                if unhandled_actions(session, position, trade_date_of(now)):
                    if not held:
                        held.append(self._held_shares())
                    qty = self._capped(session, position, qty, held[0])
                sent = self._sell_qty(session, SaleOrder(position, FLATTEN, qty), now, sending)
            except Exception:  # one position's failure must not stop the others being sold
                session.rollback()
                logger.exception("flatten of position %s failed", position.id)
                report.failed += 1
                continue
            if sent is not None and sent.status != "rejected":
                report.sent += 1
        return report

    def _held_shares(self) -> dict[str, int] | None:
        """Shares the broker holds per symbol; None when the book cannot be read."""
        try:
            holdings = self._broker.holdings()
        except BrokerUnavailableError:
            logger.warning("holdings unreadable: flatten sells the open quantity")
            return None
        out: dict[str, int] = {}
        for holding in holdings:
            out[holding.symbol] = out.get(holding.symbol, 0) + holding.qty
        return out

    @staticmethod
    def _capped(
        session: Session, position: HatPosition, qty: int, held: dict[str, int] | None
    ) -> int:
        if held is None:
            return qty  # unreadable: the broker refuses an oversell
        symbol = str(
            session.scalar(
                select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
            )
        )
        capped = max(0, min(qty, held.get(symbol, 0)))
        held[symbol] = held.get(symbol, 0) - capped  # a second position of it gets the rest
        return capped

    # --- sales --------------------------------------------------------------------------------

    def _sell(
        self,
        session: Session,
        position_id: uuid.UUID,
        action: exits.ExitAction,
        now: datetime,
        sending: Clock,
    ) -> SendOutcome | None:
        position = session.get(HatPosition, position_id)
        if position is None:
            return None
        session.refresh(position)
        purpose = EXIT_FULL if action.kind == "full" else EXIT_PARTIAL
        qty = position.qty_open if action.kind == "full" else min(action.qty, position.qty_open)
        order = SaleOrder(position, purpose, qty, tuple(action.rule_ids))
        if order.rule == snapshot.HARD_STOP and self._broker_stop_rests(session, position.id):
            # The stop resting at the broker is the hard stop; HAT sells at market only when no
            # stop the broker confirmed rests (HAT-EXEC-065).
            return None
        return self._sell_qty(session, order, now, sending)

    def _broker_stop_rests(self, session: Session, position_id: uuid.UUID) -> bool:
        """A stop the broker confirmed rests under the position, once any stop whose answer was
        lost is settled. One still unknown does not count: should it rest after all, the broker
        refuses the market sale (the shares are held for it), the safe way round."""
        self._desk.settle_lost(session, position_id)
        return any(row.broker_order_id for row in self._desk.stops_of(session, position_id, *LIVE))

    def _sell_qty(
        self, session: Session, order: SaleOrder, now: datetime, sending: Clock
    ) -> SendOutcome | None:
        """Carry out `order` (an exit or a flatten) when `sale_gate` lets it go. Every refusal is
        alerted; its `rule_ids` are marked fired once the sale is out. The position's write lock
        (`PositionDesk.lock`) is held from the "nothing else is selling" check until the sale is
        sent, so neither another sale nor a stop path (the fill sync's `protect`) interleaves;
        the position is re-read under the lock and the quantity capped at what is still open."""
        position = order.position
        with self._positions.lock(position.id, "sale"):
            session.refresh(position)
            if min(order.qty, position.qty_open) <= 0 or position.closed_at is not None:
                return None
            return self._sell_locked(session, order, now, sending)

    def _sell_locked(
        self, session: Session, order: SaleOrder, now: datetime, sending: Clock
    ) -> SendOutcome | None:
        position = order.position
        signal = session.get(HatSignal, position.signal_id)
        bot = session.get(HatBot, position.bot_id)
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
        )
        if signal is None or bot is None or symbol is None:
            return None
        settled = self._settle_sales(session, position, signal.bot_id, now)
        # a sale settled from holdings may have lowered the open quantity
        order = replace(order, qty=min(order.qty, position.qty_open))
        gate = self._gate(session, order, signal, settled, now, sending)
        if gate.step is SaleStep.REFUSE:
            self._not_placed(signal, position, str(gate.code), now)
            return None
        if gate.step is SaleStep.HOLD:
            return None
        if not self._desk.make_room(session, position.id, position.qty_open - order.qty, now):
            self._not_placed(signal, position, "stop_not_moved", now)
            return None
        request = OrderRequest(
            bot_id=signal.bot_id,
            signal_id=signal.id,
            broker_account_id=bot.broker_account_id,
            purpose=order.purpose,
            side=Side.SELL,
            qty=order.qty,
            order_type=OrderType.MARKET,
            position_id=position.id,
            rule_ids=order.rule_ids,
        )
        return self._send_sale(session, _Sale(order, signal, request), gate, now)

    def _gate(
        self,
        session: Session,
        order: SaleOrder,
        signal: HatSignal,
        settled: str,
        now: datetime,
        sending: Clock,
    ) -> SaleGate:
        """`sale_gate` over what is known of the sale now; the books it needs are read only
        when the earlier rules let it through."""
        position = order.position
        through = settled == SETTLED and order.qty > 0
        busy = through and (
            self._desk.exit_working(session, position.id)
            or bool(self._desk.triggered_gtts(session, position.id, now))
        )
        through = through and not busy
        # Checked at the moment of sending: a close job that started at 15:24 still sends
        # nothing from 15:30 on (HAT-EXEC-074).
        open_now = through and self._orders_allowed(sending.now())
        rejected = rejected_sales(session, signal.id, order.purpose, now) if open_now else 0
        floor = self._floor(session, position, now) if open_now else None
        return sale_gate(
            settled, order.qty, busy=busy, open_now=open_now, rejected=rejected, floor=floor
        )

    def _floor(self, session: Session, position: HatPosition, now: datetime) -> Decimal | None:
        """The day's lower circuit price of the position's instrument; None when unknown."""
        if self._band is None:
            return None
        return self._band.lower(session, position.instrument_id, now)

    def _settle_sales(
        self, session: Session, position: HatPosition, bot_id: uuid.UUID, now: datetime
    ) -> str:
        """Settle the position's sales of unknown outcome before it is sold again (under its
        position lock, so nothing else is sending them): one reserved and never sent is cancelled;
        one sent with its answer lost is looked up by its tag once `settle_after` has passed
        (found: adopted; absent: it never landed, ends `expired` and its rules fire again).
        One found with shares sold is left to the fill sync, which records its fills with it
        (the position's open quantity is stale until then). The order book is today's, so a
        lost sale of an earlier session is settled from the broker's holdings instead
        (`_settle_earlier`). UNREADABLE (the order book, or the holdings such a sale needs,
        could not be read), WAIT (sent too recently, or fills not yet recorded) or SETTLED, the
        first that applies."""
        rows = list(
            session.scalars(
                select(HatOrder).where(
                    HatOrder.position_id == position.id,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.purpose.in_(EXIT_SALES),
                    HatOrder.status.in_(UNSURE),
                )
            )
        )
        lost = []
        for row in rows:
            if row.status == "pending" and row.placed_at is None:
                row.status = "cancelled"  # reserved, never sent
            else:
                lost.append(row)
        session.commit()
        if not lost:
            return SETTLED
        try:
            book = {order.tag: order for order in self._broker.orders()}
        except BrokerUnavailableError:
            return UNREADABLE
        verdicts = [self._settle_one(session, row, book.get(row.tag), bot_id) for row in lost]
        stale = [row for row, v in zip(lost, verdicts, strict=True) if v is Verdict.STALE]
        if stale and not self._settle_earlier(session, position, stale, now):
            return UNREADABLE
        return WAIT if Verdict.WAIT in verdicts else SETTLED

    def _settle_one(
        self, session: Session, row: HatOrder, found: BrokerOrder | None, bot_id: uuid.UUID
    ) -> Verdict:
        """One lost sale against today's book (`settle_verdict`): adopted, ended, or left
        (WAIT: sent too recently, or found with shares sold, for the fill sync to adopt with its
        fills; STALE: of an earlier session)."""
        filled = 0 if found is None else found.filled_qty
        verdict = self._positions.verdict(
            row.placed_at, found is not None, filled_qty=filled, sale=True
        )
        if found is not None and verdict is Verdict.ADOPT:
            adopt_broker_order(session, row, found, bot_id, clock=self._clock)
        elif verdict is Verdict.EXPIRE:
            end_lost_order(session, row)
        else:
            return verdict
        session.commit()
        return verdict

    def _settle_earlier(
        self, session: Session, position: HatPosition, rows: list[HatOrder], now: datetime
    ) -> bool:
        """Settle lost sales of an earlier session from the broker's holdings: a DAY market sale
        cannot still rest, so it sold or it did not. The shares held of the symbol, less those the
        account's other open HAT positions of it hold and less the owner's own (the holding baseline
        a reconciliation recorded by the first sale's send, Q12), are this position's
        (`earlier_sale_verdict`); with no baseline recorded by then nothing changes and False is
        returned (the sale is refused, `sale_unresolved`), so the owner's shares are never read as
        HAT's and sold: the owner books the sale (`admin resolve-sale`). Never sold: the sales end
        `expired` and their rules fire again. Sold, or unsure: the sales end `filled` (their fills
        are not in today's book), the open quantity drops to the shares held
        (`position.qty_corrected`, never raised), the stop is resized to it (cancelled at none) and
        the owner is told once (`sale.settled_from_holdings`); the exit then sells what is held, and
        the owner books the sale's fill from the contract note (`admin resolve-sale`). The shares
        held are the holdings plus today's net CNC position (`broker_quantities`, as reconciliation
        reads them), so another position bought today counts on both sides. Runs under the bot's
        lock, as fills do, on the position re-read. False, with nothing changed, when the holdings
        or positions cannot be read or no baseline is known."""
        try:
            held = broker_quantities(self._broker.positions(), self._broker.holdings())
        except BrokerUnavailableError:
            logger.warning("holdings unreadable: lost sales of an earlier session wait")
            return False
        lock_bot(session, position.bot_id)
        session.refresh(position)
        symbol = str(
            session.scalar(
                select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
            )
        )
        account_id = session.scalar(
            select(HatBot.broker_account_id).where(HatBot.id == position.bot_id)
        )
        others = session.scalar(
            select(func.coalesce(func.sum(HatPosition.qty_open), 0)).where(
                HatPosition.id != position.id,
                HatPosition.instrument_id == position.instrument_id,
                HatPosition.bot_id.in_(self._bots(session, account_id)),
                HatPosition.is_virtual.is_(False),
                HatPosition.closed_at.is_(None),
            )
        )
        sent = min((as_utc(row.placed_at) for row in rows if row.placed_at), default=now)
        baseline = baseline_at(session, account_id, symbol, sent) if account_id else None
        if baseline is None:
            self._baseline_unknown(position, rows, now)
            return False
        own = max(0, held.get(symbol, 0) - int(others or 0) - baseline)
        verdict = earlier_sale_verdict(position.qty_open, own, sum(row.qty for row in rows))
        logger.info("lost sales %s of an earlier session: %s", [row.tag for row in rows], verdict)
        if verdict == NEVER_SOLD:
            for row in rows:
                end_lost_order(session, row)
            session.commit()
            return True
        for row in rows:
            row.status = "filled"
        before, after = position.qty_open, min(position.qty_open, own)
        record(
            session,
            aggregate_type="position",
            aggregate_id=position.id,
            event_type=EVENT_QTY_CORRECTED,
            actor="engine",
            payload={
                "from": before,
                "to": after,
                "held": own,
                "verdict": verdict,
                "tags": [row.tag for row in rows],
            },
            correlation_id=position.signal_id,
            bot_id=position.bot_id,
            occurred_at=now,
        )
        position.qty_open = after
        session.commit()
        self._desk.protect(session, position.id, now)  # the stop to the shares held (none: off)
        self._notifier.alert(
            ALERT_SALE_SETTLED,
            trade_date_of(now),
            {
                "bot_id": str(position.bot_id),
                "position_id": str(position.id),
                "code": verdict,
                "qty_from": before,
                "qty_to": after,
            },
        )
        return True

    def _baseline_unknown(self, position: HatPosition, rows: list[HatOrder], now: datetime) -> None:
        """No holding baseline recorded by the lost sales' send (Q12): the shares held cannot be
        told from the owner's own, so nothing is changed and the position's sales are refused
        (`sale_unresolved`) until the owner books the sale (`admin resolve-sale`); told once."""
        logger.warning("lost sales %s: no holding baseline", [row.tag for row in rows])
        if position.id in self._baseline_alerted:
            return
        self._baseline_alerted.add(position.id)
        self._notifier.alert(
            ALERT_SALE_SETTLED,
            trade_date_of(now),
            {
                "bot_id": str(position.bot_id),
                "position_id": str(position.id),
                "code": "baseline_unknown",
                "tags": ",".join(row.tag for row in rows),
            },
        )

    def _send_sale(
        self, session: Session, sale: _Sale, gate: SaleGate, now: datetime
    ) -> SendOutcome | None:
        """Carry out a SEND gate: the market sale for each attempt left in the rejection budget
        (each refusal alerted), then, refused that often with the lower band known, once as a
        LIMIT at it (PR 48): the lowest price that can trade today, so no protection band or
        price check refuses it, and it fills against any buyer; the owner is told. Refused
        too, the sale is not tried again this session (`exit.not_placed`) and the stop that made
        room for it is put back."""
        sent: SendOutcome | None = None
        for _ in range(gate.attempts):
            outcome, done = self._attempt(session, sale, sale.request, now)
            sent = outcome or sent
            if done:
                return sent
        if gate.floor is not None:
            self._notifier.alert(
                ALERT_SALE_UNFILLED,
                trade_date_of(now),
                {**self._detail(sale), "code": BAND_LIMIT},
            )
            limit = replace(sale.request, order_type=OrderType.LIMIT, price=gate.floor)
            outcome, done = self._attempt(session, sale, limit, now)
            sent = outcome or sent
            if done:
                return sent
        self._not_placed(sale.signal, sale.order.position, "order_rejected", now)
        self._desk.protect(session, sale.order.position.id, now)
        return sent

    def _attempt(
        self,
        session: Session,
        sale: _Sale,
        request: OrderRequest,
        now: datetime,
    ) -> tuple[SendOutcome | None, bool]:
        """One send of `request`: its outcome (None when nothing was sent), and whether the sale
        is done (out, unknown, or refused before the broker took it); a sale the broker rejected
        is alerted and not done."""
        signal, position = sale.signal, sale.order.position
        try:
            tag = reserve_order(session, request)
            sent = send_order(session, self._sender, tag, rule=sale.order.rule)
        except (OrderRefusedError, BrokerUnavailableError) as exc:
            session.rollback()
            code = getattr(exc, "code", "broker_unavailable")
            self._release(session, signal.id, request.purpose)
            self._not_placed(signal, position, code, now)
            self._desk.protect(session, position.id, now)  # the stop made room for it
            return None, True
        except UnresolvedOrderError:
            return None, True
        if sent.status != "rejected":
            # Placed, or unknown (looked up by tag before the position is sold again): the
            # rule has fired; it fires again if the sale ends without selling.
            if sale.order.rule_ids:
                live_trades.mark_fired(session, position.id, sale.order.rule_ids)
            session.commit()
            return sent, True
        self._notifier.alert(
            ALERT_SALE_REJECTED,
            trade_date_of(now),
            {"signal_id": str(signal.id), "tag": tag, "code": sent.code},
        )
        return sent, False

    @staticmethod
    def _detail(sale: _Sale) -> dict[str, str]:
        return {
            "bot_id": str(sale.signal.bot_id),
            "signal_id": str(sale.signal.id),
            "position_id": str(sale.order.position.id),
        }

    @staticmethod
    def _release(session: Session, signal_id: uuid.UUID, purpose: str) -> None:
        """Close a reserved sale that provably never reached the broker as cancelled."""
        for row in session.scalars(
            select(HatOrder).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == purpose,
                HatOrder.status == "pending",
                HatOrder.placed_at.is_(None),
            )
        ):
            row.status = "cancelled"
        session.commit()

    def _not_placed(
        self, signal: HatSignal, position: HatPosition, reason: str, now: datetime
    ) -> None:
        self._notifier.alert(
            ALERT_EXIT_NOT_PLACED,
            trade_date_of(now),
            {
                "bot_id": str(signal.bot_id),
                "signal_id": str(signal.id),
                "position_id": str(position.id),
                "reason": reason,
            },
        )

    # --- exits.close (15:20-15:25) ------------------------------------------------------------

    def close_exits(
        self, session: Session, account_id: uuid.UUID, views: ViewSource, now: datetime
    ) -> CloseReport:
        """The `exits.close` job body: the close-phase rules of every open position of the
        account's bots on the live price (HAT-EXEC-074), each `close_below_sma` evaluation
        recorded with that price (HAT-OPS-034). Refused outside 15:20-15:25 IST or the session."""
        report = CloseReport()
        if not in_close_window(now) or not self._orders_allowed(now):
            report.refused = "outside_close_window"
            return report
        sending = self._sending_clock(now)
        for position in self.open_positions(session, account_id, real_only=False):
            try:
                self._close_one(session, position, views, now, sending, report)
            except Exception:
                # One position's failure leaves the others evaluated; the job fails and its
                # retry runs again (fired rules and the evaluation record are not repeated).
                session.rollback()
                logger.exception("close-phase exits of position %s failed", position.id)
                report.failed += 1
        return report

    def _close_one(
        self,
        session: Session,
        position: HatPosition,
        views: ViewSource,
        now: datetime,
        sending: Clock,
        report: CloseReport,
    ) -> None:
        view = views(session, position)
        if view is None or view.price is None:
            return  # no live price: nothing fires (missing data never fires a rule)
        if position.is_virtual:
            result = shadow.evaluate_position(session, position.id, view, "close", now)
            session.commit()
        else:
            result = self._evaluate(session, position.id, view, "close", now, sending)
            report.sales += len(result.actions)
        report.evaluated += 1
        day = trade_date_of(now)
        if close_gap.record_close_evaluation(session, position.id, view, result, day, now):
            report.recorded += 1
        session.commit()

    # --- gtt.swap (~15:25) and gtt.verify (nightly) -------------------------------------------

    def swap_stops(self, session: Session, account_id: uuid.UUID, now: datetime) -> StopsReport:
        """The `gtt.swap` job body: every open real position's intraday stop becomes a GTT."""
        report = StopsReport()
        for position in self.open_positions(session, account_id):
            outcome = self._upkeep.swap(session, position.id, now)
            if outcome == SWAPPED:
                report.done += 1
            elif outcome != CLOSED:
                report.failed.append(position.id)
        return report

    def sync(
        self, session: Session, account_id: uuid.UUID, now: datetime, *, check_gtts: bool = False
    ) -> SyncReport | None:
        """Bring down to its stop the broker stop of any position a split or bonus rescaled,
        then read the broker's fills into HAT's book (never waiting for a reconciliation). The
        trigger moves first: the fill sync resizes a stop to the open quantity at its trigger, and
        a full-size stop at the old trigger would sell the position at the open."""
        self._follow_rescales(session, account_id, now)
        try:
            report = self._fills.run(session, account_id, now, check_gtts=check_gtts)
        except BrokerUnavailableError:
            return None
        if self._orders_allowed(now):
            self._flatten_again(session, account_id, now)
        self._mpp.after_sync(session, report, now, self)
        return report

    def _flatten_again(self, session: Session, account_id: uuid.UUID, now: datetime) -> None:
        """Sell the late entry fill of each position a flatten emptied (`flatten_owed`, Q13): a
        follow-up flatten sale under the original confirmation, through the exit path (the same
        session, lock and stop checks), once the fill sync has protected the shares. A sale the
        gate holds is asked again at the next sync. One position's failure leaves the others."""
        for position in self.open_positions(session, account_id):
            try:
                if live_trades.flatten_owed(session, position):
                    order = SaleOrder(position, FLATTEN, position.qty_open)
                    self._sell_qty(session, order, now, self._sending_clock(now))
            except Exception:
                session.rollback()
                logger.exception("follow-up flatten of position %s failed", position.id)

    # --- market price protection (PR 48) ------------------------------------------------------

    def sell_again(
        self, session: Session, position: HatPosition, row: HatOrder, qty: int, now: datetime
    ) -> None:
        """A fresh market sale of `qty` shares under the purpose and rules of the sale `row`
        that HAT cancelled to send again (`mpp.MarketProtection`), through the exit path (the
        same session, lock and stop checks)."""
        order = SaleOrder(position, row.purpose, qty, tuple(row.rule_ids or ()))
        self._sell_qty(session, order, now, self._sending_clock(now))

    def _follow_rescales(self, session: Session, account_id: uuid.UUID, now: datetime) -> None:
        """`StopDesk.rescaled` for every open real position (HAT-EXEC-143); one position's
        failure leaves the others tried, and the next sync tries it again."""
        for position in self.open_positions(session, account_id):
            try:
                self._desk.rescaled(session, position.id, now)
            except BrokerUnavailableError:
                session.rollback()
                return
            except Exception:
                session.rollback()
                logger.exception("rescaled stop of position %s failed", position.id)

    def verify_stops(self, session: Session, account_id: uuid.UUID, now: datetime) -> StopsReport:
        """The `gtt.verify` job body (HAT-EXEC-063, HAT-OPS-024): the broker's fills first, then
        every open real position's GTT refreshed, or alerted and placed again."""
        report = StopsReport()
        # Fills unread (the trade book unreadable): the open quantities may be stale,
        # so no GTT is modified or placed; each position is alerted `gtt.unverified`.
        fresh = self.sync(session, account_id, now) is not None
        for position in self.open_positions(session, account_id):
            self._alert_series_change(session, position, now)
            outcome = (
                self._upkeep.verify(session, position.id, now)
                if fresh
                else self._upkeep.unverified(session, position.id, now, "fills_unread")
            )
            if outcome == FAILED:
                report.failed.append(position.id)
            elif outcome in (VERIFIED, REPLACED):
                report.done += 1
        return report

    # --- gtt.check (08:40) and series changes ------------------------------------------------

    def _alert_series_change(self, session: Session, position: HatPosition, now: datetime) -> bool:
        """URGENT `position.series_changed` for an open position whose instrument moved to
        another series (HAT-EXEC-146); True when it did."""
        moved = series_changed(session, position)
        if moved is None:
            return False
        self._notifier.alert(
            ALERT_SERIES_CHANGED,
            trade_date_of(now),
            {
                "bot_id": str(position.bot_id),
                "position_id": str(position.id),
                "symbol": moved.symbol,
                "from": moved.previous_series or "",
                "to": moved.series,
                "broker_symbol": broker_symbol(session, moved.id) or "unmapped",
            },
        )
        return True

    def check_gtts(self, session: Session, account_id: uuid.UUID, now: datetime) -> StopsReport:
        """The `gtt.check` job body (HAT-EXEC-145, HAT-EXEC-146): a fill sync that reads the
        broker's GTT book whatever its cadence (`FillSync` step 6c ends and replaces each GTT the
        broker dropped), then each open real position whose instrument changed series is alerted
        and its GTT checked against the same book (`GttUpkeep.check_gtt` replaces one under the old
        series' symbol). The run fails, so the scheduler tries again, while the fills or the GTT
        book cannot be read (nothing is placed or ended then) or a position is left unprotected;
        a GTT the broker refuses is alerted (`stop.not_placed`) and the fill sync tries again."""
        report = StopsReport()
        synced = self.sync(session, account_id, now, check_gtts=True)
        # after the sync, so a position its fills closed is not alerted
        moved = [
            position
            for position in self.open_positions(session, account_id)
            if self._alert_series_change(session, position, now)
        ]
        if synced is None or synced.gtt_book is None or synced.gtt_read_at is None:
            report.unread = True
            return report
        report.failed.extend(synced.gtt_unprotected)
        report.done += len(synced.protected)
        for position in moved:
            outcome = self._upkeep.check_gtt(
                session, position.id, now, synced.gtt_book, synced.gtt_read_at
            )
            if outcome in (UNVERIFIED, FAILED):
                report.failed.append(position.id)
            elif outcome == REPLACED:
                report.done += 1
        return report

    # --- the session end ----------------------------------------------------------------------

    def end_session(self, session: Session, account_id: uuid.UUID, now: datetime) -> int:
        """After the close: an entry whose order may have been sent but that the broker's book
        does not hold ends its signal Rejected (`entry_not_found`); a reserved entry that was
        never sent is cancelled with it. A sale of unknown outcome the book does not hold ends
        `expired` (cancelled when never sent) and its rules fire again (a DAY market sale cannot
        fill after the close). An order sent less than `settle_after` ago is left for the next run
        (HAT-EXEC-065). Returns the number of signals ended. Nothing is ended when the book cannot
        be read."""
        rows = list(
            session.scalars(
                select(HatOrder).where(
                    HatOrder.broker_account_id == account_id,
                    HatOrder.purpose.in_((ENTRY, *EXIT_SALES)),
                    HatOrder.is_virtual.is_(False),
                    HatOrder.status.in_(UNSURE),
                )
            )
        )
        if not rows:
            return 0
        try:
            book = {o.tag for o in self._broker.orders()}
        except BrokerUnavailableError:
            return 0
        ended = 0
        for row in rows:
            sale = row.purpose != ENTRY
            if self._positions.verdict(row.placed_at, row.tag in book, sale=sale) is not (
                Verdict.EXPIRE
            ):
                # held: the next sync adopts it; young: it may still be on its way; a sale of
                # an earlier session: today's book cannot show it, settled from holdings
                continue
            if sale:
                if row.placed_at is None:
                    row.status = "cancelled"
                else:
                    end_lost_order(session, row)
                session.commit()
                continue
            row.status = "cancelled" if row.placed_at is None else "expired"
            if entries.reject_signal(session, row.signal_id, ENTRY_NOT_FOUND, now):
                ended += 1
            session.commit()
        return ended
