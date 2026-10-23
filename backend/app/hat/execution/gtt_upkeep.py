"""The GTTs that rest under real positions outside the session (HAT-EXEC-061, -063, -145, -146,
HAT-OPS-024; the model is `verification/tla/StopPlacement.tla`).

`GttUpkeep` runs over an account's `StopDesk` and its write steps:

- **At ~15:25** (`swap`): the GTT is placed first and the intraday stop is cancelled only once
  the GTT is confirmed placed; a GTT that fails leaves the intraday stop working and raises
  `gtt.swap_failed`. A position with a working exit order is not swapped (its exit comes first).
- **Nightly** (`verify`): every open real position's GTT is refreshed to the current stop
  (`gtt.refreshed`). A GTT the broker no longer knows (triggered, cancelled or never confirmed)
  is alerted (`gtt.missing`) and placed again; when the broker cannot be reached the position
  is alerted (`gtt.unverified`) and nothing is placed, since the old GTT may still rest there.
  A GTT placed `GTT_RENEW_AFTER` (300 days) ago or more is cancelled and placed again (the
  broker expires a GTT after a year, and a modify may not reset that clock; PR 47).
- **A GTT the broker dropped** (`check_gtt`, HAT-EXEC-145, P1): Fyers cancels a resting GTT
  before a corporate action's ex-date (a dividend's too) and on a series change, and the owner
  can cancel one in the Fyers app. The broker's GTT book is read at 08:40 and by the fill sync
  every few minutes; a live GTT a good read no longer holds (`stop_confirmed`) is ended
  `cancelled`, alerted once (`gtt.missing`) and the position protected again at once: a GTT
  outside the session, an intraday stop inside it. Until then the engine would trust it as the
  hard stop (`TradeManager._broker_stop_rests`); once ended, the engine sells on its own.

Each step holds the position's lock (`position_desk.serialised`), as every stop write does.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..engine.rescale import pending_actions
from ..models import HatOrder
from ..time import as_utc, trade_date_of
from .broker import CODES_NOT_SENT, GttModify
from .gtt import gtt_limit_price
from .position_desk import PositionDesk, serialised
from .protect import (
    ALERT_GTT_MISSING,
    ALERT_GTT_SWAP_FAILED,
    ALERT_GTT_UNVERIFIED,
    CLOSED,
    EVENT_GTT_REFRESHED,
    EXIT_WORKING,
    FAILED,
    PLACED,
    PROTECTED,
    REPLACED,
    SWAPPED,
    UNVERIFIED,
    VERIFIED,
    StopDesk,
    StopTarget,
)
from .series import series_changed
from .stop_plans import (
    LIVE,
    STOP_DAY,
    STOP_GTT,
    UNSURE,
    WORKING,
    VerifyStep,
    broker_trigger,
    live_gtts,
    lost,
    raise_only,
    stop_confirmed,
    verify_plan,
)

__all__ = ["GttUpkeep"]

GTT_CANCELLED_BY_BROKER = "gtt_cancelled_by_broker"  # a good GTT-book read no longer holds it


class GttUpkeep:
    """The swap, verify and GTT-book check of one broker account's real positions."""

    def __init__(self, desk: StopDesk) -> None:
        self._desk = desk

    @property
    def positions(self) -> PositionDesk:
        """The account's position locks (`serialised`)."""
        return self._desk.positions

    # --- the 15:25 swap -----------------------------------------------------------------------

    @serialised
    def swap(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """Replace the intraday stop with a GTT for the night (HAT-EXEC-061): the GTT first, the
        intraday stop cancelled only once the GTT is confirmed placed."""
        target = self._desk.target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        if self._desk.exit_working(session, position.id):
            return EXIT_WORKING  # the sale comes first; the job tries the swap again
        has_gtt = self._has_live_gtt(session, position.id)
        if not has_gtt:
            self._desk.settled_stops(session, target, STOP_GTT)
            has_gtt = self._has_live_gtt(session, position.id)
            unsure = self._unsure_gtts(session, position.id)
            has_gtt = has_gtt or any(self._desk.resend(session, target, row) for row in unsure)
            if not has_gtt and unsure:
                self._desk.alert(ALERT_GTT_SWAP_FAILED, target, now, "gtt_unconfirmed")
                return FAILED
        if not has_gtt and not self._desk.place(
            session, target, STOP_GTT, now, ALERT_GTT_SWAP_FAILED
        ):
            return FAILED  # the intraday stop keeps working; alerted
        # Every day stop is cancelled, even after one fails. A day stop not cancelled (refused,
        # or its outcome unknown) would sell the same shares as the GTT: the job fails and tries
        # the cancel again.
        cancelled = True
        for row in self._desk.stops_of(session, position.id):
            if row.purpose == STOP_DAY and not self._desk.cancel(session, target, row, now):
                cancelled = False
        return SWAPPED if cancelled else FAILED

    def _has_live_gtt(self, session: Session, position_id: uuid.UUID) -> bool:
        return any(
            row.purpose == STOP_GTT and row.broker_order_id
            for row in self._desk.stops_of(session, position_id, *LIVE)
        )

    def _unsure_gtts(self, session: Session, position_id: uuid.UUID) -> list[HatOrder]:
        return [
            r for r in self._desk.stops_of(session, position_id, *UNSURE) if r.purpose == STOP_GTT
        ]

    # --- the nightly verify -------------------------------------------------------------------

    def _settled_gtts(self, session: Session, target: StopTarget) -> list[HatOrder]:
        """The position's working GTTs, after settling those whose answer was lost."""
        rows = self._desk.settled_stops(session, target, STOP_GTT)
        return [row for row in rows if row.purpose == STOP_GTT]

    def _old_series(self, session: Session, target: StopTarget, row: HatOrder) -> bool:
        """The GTT went out under the old series' symbol of a position whose instrument changed
        series (HAT-EXEC-146): placed on or before the day the refresh saw the new series
        (`series_since`). That symbol takes no order, so a GTT the broker still lists under it
        protects nothing. The weekly refresh runs on a Saturday, when no GTT is placed; one placed
        on that day is counted old, so at worst it is placed again once. With no Fyers symbol for
        the new series nothing could replace it, so it is left for the owner (the URGENT alert
        says `unmapped`)."""
        moved = None if target.unmapped else series_changed(session, target.position)
        if moved is None:
            return False
        if row.placed_at is None or moved.series_since is None:
            return True
        return trade_date_of(as_utc(row.placed_at)) <= moved.series_since

    @serialised
    def check_gtt(
        self,
        session: Session,
        position_id: uuid.UUID,
        now: datetime,
        book: Collection[str],
        read_at: datetime,
    ) -> str:
        """The position's GTT against the broker's GTT book (HAT-EXEC-145): `book` holds the ids
        the book showed (resting or triggered) when it was read at `read_at`. A live GTT of the
        position a good read does not hold (`stop_confirmed`) was cancelled or expired by the
        broker: it is ended `cancelled` (reason `gtt_cancelled_by_broker` on its event), alerted
        (`gtt.missing`) and the position protected again (`protect`: a GTT outside the session,
        an intraday stop inside it; a refused one is alerted `stop.not_placed` and the fill sync
        tries again). One placed too recently for the book to show counts as resting. A GTT
        whose answer was lost is settled first; one still unsure is alerted (`gtt.unverified`)
        and nothing is placed on top of it. A position still awaiting a split or bonus rescale
        gets nothing yet (UNVERIFIED, so the job tries again; `ca.rescale` alerts it): a GTT at
        its old-scale stop would rest above the ex-date price and sell at the open. A GTT still
        listed under the old series' symbol (HAT-EXEC-146) is cancelled and placed again under
        the new one; a refused cancel is alerted (`gtt.unverified`) and nothing is placed."""
        target = self._desk.target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        gtts = [
            r for r in self._desk.settled_stops(session, target, STOP_GTT) if r.purpose == STOP_GTT
        ]
        live = [row for row in gtts if row.status in LIVE and row.broker_order_id]
        stale = [row for row in live if self._old_series(session, target, row)]
        if any(
            stop_confirmed(row, book, read_at, self.positions.settle_after) is not False
            for row in live
            if row not in stale
        ):
            return VERIFIED
        if any(lost(row) for row in gtts):
            self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, "gtt_unconfirmed")
            return UNVERIFIED
        if pending_actions(session, position, trade_date_of(now)):
            return UNVERIFIED
        for row in stale:
            if row.broker_order_id in book and not self._desk.cancel(session, target, row, now):
                self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, "series_cancel_refused")
                return UNVERIFIED
        live = [row for row in live if row.status in LIVE]  # the broker cancelled these
        if live:
            self._desk.alert(ALERT_GTT_MISSING, target, now, GTT_CANCELLED_BY_BROKER)
            for row in live:
                row.status = "cancelled"
                self._desk.order_event(
                    session, row, target, "order.cancelled", now, GTT_CANCELLED_BY_BROKER
                )
            session.commit()
        outcome = self._desk.protect(session, position_id, now)
        if outcome == PLACED:
            return REPLACED
        return VERIFIED if outcome == PROTECTED else FAILED

    def unverified(
        self, session: Session, position_id: uuid.UUID, now: datetime, reason: str
    ) -> str:
        """The verify could not run for the position (its fills unread): `gtt.unverified` is
        alerted and its stops are left as they are."""
        target = self._desk.target(session, position_id)
        self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, reason)
        return FAILED

    @serialised
    def verify(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """Confirm the position's GTT rests at the broker at the current stop (HAT-EXEC-063,
        HAT-OPS-024), one step of `verify_plan` at a time: refreshed, else alerted and placed
        again. A GTT under the old series' symbol (HAT-EXEC-146), or placed `GTT_RENEW_AFTER` ago
        (research G24; the market is shut at the nightly verify, so nothing can trigger
        meanwhile), is cancelled and placed again; a refused cancel leaves it to the next step.
        A position whose split or bonus is not rescaled yet is left as it is: its stop and
        quantity are on the old scale (HAT-EXEC-142)."""
        target = self._desk.target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        if pending_actions(session, position, trade_date_of(now)):
            # A GTT placed at the old-scale stop would rest above the price: told, nothing done.
            self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, "corporate_action_pending")
            return UNVERIFIED
        tick = self._desk.tick(session, position)
        goal = broker_trigger(Decimal(position.current_stop), tick)
        gtts = [
            r for r in self._desk.settled_stops(session, target, STOP_GTT) if r.purpose == STOP_GTT
        ]
        live = live_gtts(gtts)
        old_series = bool(live) and tick is not None and self._old_series(session, target, live[-1])
        known = None if tick is None else goal
        step = self._verify_step(session, target, gtts, known, old_series, now)
        if step in (REPLACED, FAILED):
            return step  # a GTT to replace was cancelled and placed again
        if step is VerifyStep.NO_TICK:
            # No GTT price can be set without the tick: told, and nothing is ended or placed.
            self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, "no_tick")
            return UNVERIFIED
        if step is VerifyStep.UNSURE:
            # A GTT whose answer was lost that the GTT book could not settle (unreadable, or
            # sent too recently) may rest at the broker: told, and nothing is placed.
            self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, "gtt_unconfirmed")
            return UNVERIFIED
        if step is VerifyStep.REFRESH and tick is not None:
            refreshed = self._refresh(session, target, live[-1], goal, tick, now)
            if refreshed is not None:
                return refreshed
        return self._place_missing(session, target, gtts, now)

    def _verify_step(
        self,
        session: Session,
        target: StopTarget,
        gtts: list[HatOrder],
        goal: Decimal | None,
        old_series: bool,
        now: datetime,
    ) -> VerifyStep | str:
        """The `verify_plan` step left once a GTT to replace (old series, renewal) is cancelled:
        REPLACED or FAILED when one was and a new one placed; a refused cancel goes on to the
        next step."""
        step: VerifyStep | None = None
        while step is None or step in (VerifyStep.REPLACE_OLD_SERIES, VerifyStep.RENEW):
            step = verify_plan(
                gtts,
                goal,
                old_series=old_series,
                wall=self.positions.clock.now(),  # the clock the placement was stamped with
                after=step,
            )
            if step in (VerifyStep.REPLACE_OLD_SERIES, VerifyStep.RENEW) and self._desk.cancel(
                session, target, live_gtts(gtts)[-1], now
            ):
                return REPLACED if self._desk.place(session, target, STOP_GTT, now) else FAILED
        return step

    def _place_missing(
        self, session: Session, target: StopTarget, gtts: list[HatOrder], now: datetime
    ) -> str:
        """No GTT rests at the broker (none, cancelled, triggered or ended): alerted and a new
        one placed. A row left open is ended first so it is never sent again."""
        self._desk.alert(ALERT_GTT_MISSING, target, now, "no_active_gtt")
        for row in gtts:
            if row.status in WORKING:
                row.status = "expired"
        session.commit()
        return REPLACED if self._desk.place(session, target, STOP_GTT, now) else FAILED

    def _refresh(
        self,
        session: Session,
        target: StopTarget,
        row: HatOrder,
        goal: Decimal,
        tick: Decimal,
        now: datetime,
    ) -> str | None:
        """Modify the resting GTT `row` to the open quantity at `raise_only`'s trigger for
        `goal`: VERIFIED once done, UNVERIFIED (alerted) when it may still rest unchanged, None
        once the broker confirmed it cancelled (it is placed again)."""
        current = Decimal(row.trigger_price) if row.trigger_price is not None else None
        trigger = raise_only(current, goal)
        # A coarser tick that floors the stop below the resting trigger leaves the trigger, and
        # its limit, where they are (F24).
        keep = trigger != goal
        limit = gtt_limit_price(trigger, tick)
        if keep and row.price is not None:
            limit = Decimal(row.price)
        qty = target.position.qty_open
        ack = self._desk.broker.modify_gtt(GttModify(str(row.broker_order_id), qty, trigger, limit))
        if ack.ok:
            row.qty, row.trigger_price, row.price = qty, trigger, limit
            self._desk.order_event(session, row, target, EVENT_GTT_REFRESHED, now)
            session.commit()
            return VERIFIED
        # A GTT kept above the new tick's floor is never replaced by a lower one; one that may
        # still rest at the broker is never doubled, since two could sell the same shares twice.
        if keep or ack.code in CODES_NOT_SENT or not self._desk.cancel(session, target, row, now):
            self._desk.alert(ALERT_GTT_UNVERIFIED, target, now, ack.code)
            return UNVERIFIED
        return None
