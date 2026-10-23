"""What a broker stop should do next, decided without the broker (HAT-EXEC-060..065).

`StopDesk` (`protect.py`) reads the rows and does the I/O; the decisions are here, as pure
functions over the position's stop rows:

- `raise_only`: where a resting stop's trigger moves. It is the only code that decides a
  trigger change, so "a broker stop only moves up" holds by construction: `follow` and the
  nightly refresh can only raise it, and only a split or bonus rescale (`rescaled`) lowers it.
- `protect_plan`: after a fill, the one next step that keeps a stop under the open quantity.
- `verify_plan`: at night, the one next step for the position's GTT.

A step that needs the broker (a settle, a resend, a cancel) may not settle the question; the desk
then plans again with `after` set to the step it tried, and the plan goes on from there.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import overload

from ..models import HatOrder
from ..time import as_utc
from .position_desk import LOST_ANSWER_SETTLE

__all__ = [
    "GTT_RENEW_AFTER",
    "LIVE",
    "STOP_DAY",
    "STOP_GTT",
    "UNSURE",
    "WORKING",
    "ProtectPlan",
    "ProtectStep",
    "VerifyStep",
    "broker_trigger",
    "live_gtts",
    "lost",
    "protect_plan",
    "raise_only",
    "stop_confirmed",
    "verify_plan",
]

STOP_DAY = "stop_day"
STOP_GTT = "stop_gtt"
WORKING = ("pending", "unconfirmed", "placed", "partially_filled")
LIVE = ("placed", "partially_filled")
UNSURE = ("pending", "unconfirmed")
# A GTT this old is placed again by the nightly verify: Fyers expires a GTT a year after it was
# placed, and a modify may not reset that clock (research G24; [verify on a live account]).
GTT_RENEW_AFTER = timedelta(days=300)


def broker_trigger(stop: Decimal, tick: Decimal | None) -> Decimal:
    """The broker trigger of a stop: rounded down to the tick (never above the stop); the stop
    itself when the tick is unknown (stops from the engine are already on the tick)."""
    if tick is None or not tick.is_finite() or tick <= 0:
        return stop
    return (stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


@overload
def raise_only(current: Decimal | None, goal: Decimal) -> Decimal: ...
@overload
def raise_only(current: Decimal | None, goal: Decimal, *, rescaled: bool) -> Decimal | None: ...
def raise_only(current: Decimal | None, goal: Decimal, *, rescaled: bool = False) -> Decimal | None:
    """The trigger a resting stop at `current` moves to for the goal `goal`: never below
    `current`, so a stop only moves up (a coarser tick that floors the goal below the resting
    trigger leaves it where it is, F24). A split or bonus rescale (`rescaled`, HAT-EXEC-143) is
    the one move down, and it never moves up. Equal to `current` when the stop stays; None when
    a rescale finds no trigger to lower."""
    if current is None:
        return None if rescaled else goal
    return min(current, goal) if rescaled else max(current, goal)


def lost(row: HatOrder) -> bool:
    """The stop was sent and its answer lost (or the process stopped mid-send)."""
    return row.broker_order_id is None and (
        row.status == "unconfirmed" or (row.status == "pending" and row.placed_at is not None)
    )


def live_gtts(rows: Sequence[HatOrder]) -> list[HatOrder]:
    """The GTTs in `rows` the broker confirmed and that may still rest there."""
    return [r for r in rows if r.purpose == STOP_GTT and r.status in LIVE and r.broker_order_id]


def stop_confirmed(
    row: HatOrder,
    book: Collection[str] | None,
    read_at: datetime,
    settle_after: timedelta = LOST_ANSWER_SETTLE,
) -> bool | None:
    """Whether the broker's GTT book (`book`: the ids it shows resting or triggered, read at
    `read_at`) confirms the live GTT `row` rests: True when it holds the id, False when a good
    read does not (the broker cancelled or expired it). None leaves the row as it is: the book
    could not be read, the row is not a live GTT the broker gave an id, or it was placed less
    than `settle_after` before the read (the book may not show it yet)."""
    if book is None or row.status not in LIVE or not row.broker_order_id:
        return None
    if row.placed_at is None or read_at - as_utc(row.placed_at) < settle_after:
        return None
    return row.broker_order_id in book


# --- after a fill --------------------------------------------------------------------------------


class ProtectStep(StrEnum):
    """The steps of `protect_plan`, in the order they are tried."""

    CLOSED = "closed"  # nothing open: cancel the working stops
    GTT_SELLING = "gtt_selling"  # a GTT triggered and its sale is part filled: the rest may sell
    EXIT_WORKING = "exit_working"  # a sale the broker holds: the sync protects what it leaves
    SETTLE = "settle"  # a GTT whose answer was lost: settle it against the GTT book first
    COVERED = "covered"  # every live stop covers the open quantity
    RESIZE = "resize"  # live stops of another quantity: resize them
    RESEND = "resend"  # a stop never sent, or a lost day stop old enough: send it by its tag
    WAIT_LOST = "wait_lost"  # a lost answer may rest at the broker: place nothing over it
    PLACE_DAY = "place_day"
    PLACE_GTT = "place_gtt"


_PROTECT_ORDER = list(ProtectStep)


@dataclass(frozen=True)
class ProtectPlan:
    step: ProtectStep
    rows: tuple[HatOrder, ...] = ()  # the rows the step acts on (RESIZE, RESEND)
    reason: str | None = None  # WAIT_LOST: the alert code; None while every lost one is young


def protect_plan(
    rows: Sequence[HatOrder],
    open_qty: int,
    *,
    exit_live: bool,
    day_stop: bool,
    young: Callable[[datetime | None], bool],
    after: ProtectStep | None = None,
) -> ProtectPlan:
    """The next step that keeps a broker stop under the position's `open_qty` shares (0 once it
    is closed), from its working stop rows `rows`. `exit_live`: a sale of it rests at the broker.
    `day_stop`: a new stop now is an intraday order (in the session, before the swap). `young`:
    a send this recent may still land. `after`: the step just tried, which did not settle it;
    the plan goes on with the steps after it.

    A lost GTT is never sent again (the GTT book may lag one that landed), and while one may
    rest no other stop is sent; a lost day stop is looked up by its tag and sent again only when
    absent, once it is old enough."""

    def due(step: ProtectStep) -> bool:
        return after is None or _PROTECT_ORDER.index(step) > _PROTECT_ORDER.index(after)

    if open_qty <= 0:
        return ProtectPlan(ProtectStep.CLOSED)
    lost_gtt = any(lost(r) and r.purpose == STOP_GTT for r in rows)
    held = _held(rows, exit_live=exit_live, lost_gtt=lost_gtt, due=due)
    if held is not None:
        return held
    live = [r for r in rows if r.status in LIVE and r.broker_order_id]
    if due(ProtectStep.RESIZE) and live:
        resize = tuple(r for r in live if r.qty != open_qty)
        return (
            ProtectPlan(ProtectStep.RESIZE, resize) if resize else ProtectPlan(ProtectStep.COVERED)
        )
    # A GTT its settle left lost may rest at the broker: no other stop is sent over it.
    ready = tuple(r for r in rows if _ready_to_send(r, young))
    if due(ProtectStep.RESEND) and ready and not lost_gtt:
        return ProtectPlan(ProtectStep.RESEND, ready)
    return _wait_or_place(rows, day_stop=day_stop, young=young)


def _held(
    rows: Sequence[HatOrder],
    *,
    exit_live: bool,
    lost_gtt: bool,
    due: Callable[[ProtectStep], bool],
) -> ProtectPlan | None:
    """GTT_SELLING, EXIT_WORKING or SETTLE when one is due: the broker holds a sale or may hold
    a GTT, and nothing is sized or sent until it is known."""
    if due(ProtectStep.GTT_SELLING) and any(_gtt_selling(r) for r in rows):
        return ProtectPlan(ProtectStep.GTT_SELLING)
    if due(ProtectStep.EXIT_WORKING) and exit_live:
        return ProtectPlan(ProtectStep.EXIT_WORKING)
    if due(ProtectStep.SETTLE) and lost_gtt:
        return ProtectPlan(ProtectStep.SETTLE)
    return None


def _gtt_selling(row: HatOrder) -> bool:
    return row.purpose == STOP_GTT and row.status == "partially_filled"


def _ready_to_send(row: HatOrder, young: Callable[[datetime | None], bool]) -> bool:
    """A stop reserved and never sent, or a day stop whose answer was lost, old enough to be
    looked up by its tag and sent again only when absent."""
    if row.status not in UNSURE:
        return False
    return not lost(row) or (row.purpose == STOP_DAY and not young(row.placed_at))


def _wait_or_place(
    rows: Sequence[HatOrder], *, day_stop: bool, young: Callable[[datetime | None], bool]
) -> ProtectPlan:
    """WAIT_LOST while a lost answer may rest at the broker (alerted only once one is no longer
    in flight: alerting a young one on every sync would only add noise); else a new stop."""
    unsure = [r for r in rows if lost(r)]
    if not unsure:
        return ProtectPlan(ProtectStep.PLACE_DAY if day_stop else ProtectStep.PLACE_GTT)
    if all(young(r.placed_at) for r in unsure):
        return ProtectPlan(ProtectStep.WAIT_LOST)
    gtt = any(r.purpose == STOP_GTT for r in unsure)
    return ProtectPlan(
        ProtectStep.WAIT_LOST, reason="gtt_unconfirmed" if gtt else "stop_unconfirmed"
    )


# --- the nightly verify ---------------------------------------------------------------------------


class VerifyStep(StrEnum):
    """The steps of `verify_plan`, in the order they are tried."""

    NO_TICK = "no_tick"  # no GTT price can be set: told, nothing ended or placed
    REPLACE_OLD_SERIES = "replace_old_series"  # under the old series' symbol: cancel, place again
    RENEW = "renew"  # placed `GTT_RENEW_AFTER` ago: cancel, place again with a new date
    REFRESH = "refresh"  # modify the resting GTT to the open quantity at `raise_only`'s trigger
    UNSURE = "unsure"  # a lost GTT the book could not settle may rest there: told, nothing placed
    PLACE = "place"  # no GTT rests at the broker: told, and placed


_VERIFY_ORDER = list(VerifyStep)


def verify_plan(
    rows: Sequence[HatOrder],
    goal: Decimal | None,
    *,
    old_series: bool,
    wall: datetime,
    after: VerifyStep | None = None,
) -> VerifyStep:
    """The next step for the position's GTT rows `rows` (settled) at night. `goal`: the broker
    trigger of the current stop (None when the tick is unknown). `old_series`: the last live GTT
    went out under the old series' symbol (HAT-EXEC-146). `wall`: the clock the placement was
    stamped with. `after`: the step just tried, whose cancel the broker refused."""

    def due(step: VerifyStep) -> bool:
        return after is None or _VERIFY_ORDER.index(step) > _VERIFY_ORDER.index(after)

    if goal is None:
        return VerifyStep.NO_TICK
    live = live_gtts(rows)
    if not live:
        return VerifyStep.UNSURE if any(lost(r) for r in rows) else VerifyStep.PLACE
    if due(VerifyStep.REPLACE_OLD_SERIES) and old_series:
        return VerifyStep.REPLACE_OLD_SERIES
    if due(VerifyStep.RENEW) and _renew_due(live[-1], goal, wall):
        return VerifyStep.RENEW
    return VerifyStep.REFRESH if due(VerifyStep.REFRESH) else VerifyStep.PLACE


def _renew_due(row: HatOrder, goal: Decimal, wall: datetime) -> bool:
    """Placed `GTT_RENEW_AFTER` ago or more (research G24), and its new trigger would not rest
    below the old one (a coarser tick, F24)."""
    if row.placed_at is None or wall - as_utc(row.placed_at) < GTT_RENEW_AFTER:
        return False
    current = Decimal(row.trigger_price) if row.trigger_price is not None else None
    return raise_only(current, goal) == goal
