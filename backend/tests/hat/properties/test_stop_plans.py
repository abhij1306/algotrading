"""Plain table tests for the pure stop planners (`execution/stop_plans.py`) and the stop desk's
once-a-day alerts (`protect.AlertedToday`)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.hat.execution.broker import BrokerOrder, Side
from app.hat.execution.protect import AlertedToday
from app.hat.execution.stop_plans import (
    ProtectStep,
    VerifyStep,
    gtt_child,
    protect_plan,
    raise_only,
    retry_due,
    stop_covers,
    stops_cover,
    verify_plan,
)
from app.hat.models import HatOrder

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
D = Decimal


def stop(purpose: str = "stop_day", status: str = "placed", **kw: object) -> HatOrder:
    fields: dict[str, object] = {
        "qty": 100,
        "broker_order_id": "B-1" if status in ("placed", "partially_filled") else None,
        "placed_at": NOW - timedelta(minutes=5),
        "trigger_price": D("100"),
    }
    fields.update(kw)
    return HatOrder(purpose=purpose, status=status, **fields)


@pytest.mark.parametrize(
    ("current", "goal", "rescaled", "moved"),
    [
        (D("100"), D("101"), False, D("101")),  # up
        (D("100"), D("100"), False, D("100")),  # stays
        (D("100"), D("99.95"), False, D("100")),  # never down (a coarser tick, F24)
        (None, D("99"), False, D("99")),  # no trigger yet: the goal
        (D("100"), D("90"), True, D("90")),  # a rescale moves it down
        (D("100"), D("101"), True, D("100")),  # and never up
        (None, D("90"), True, None),  # nothing to lower
    ],
)
def test_raise_only(
    current: Decimal | None, goal: Decimal, rescaled: bool, moved: Decimal | None
) -> None:
    assert raise_only(current, goal, rescaled=rescaled) == moved


@pytest.mark.parametrize(
    ("trigger", "current_stop", "tick", "covers"),
    [
        (D("100"), D("100"), D("0.05"), True),  # at the stop
        (D("92"), D("100"), D("0.05"), False),  # a refused breakeven raise left it low
        (D("100.05"), D("100.07"), D("0.05"), True),  # the stop's trigger is rounded down
        (D("100.05"), D("100.10"), D("0.05"), False),
        (D("100"), D("100.01"), None, False),  # no tick: the stop itself
        (None, D("100"), D("0.05"), False),  # no trigger known
    ],
)
def test_stop_covers(
    trigger: Decimal | None, current_stop: Decimal, tick: Decimal | None, covers: bool
) -> None:
    assert stop_covers(trigger, current_stop, tick) is covers


@pytest.mark.parametrize(
    ("stops", "covered"),
    [
        ([(D("100"), 10)], True),
        ([(D("100"), 6)], False),  # a refused resize left it short of the open shares
        ([(D("100"), 6), (D("100"), 4)], True),  # two stops at the stop hold every share
        ([(D("100"), 6), (D("92"), 4)], False),  # one of them left low
        ([(D("92"), 10)], False),
        ([], False),
    ],
)
def test_stops_cover(stops: list[tuple[Decimal, int]], covered: bool) -> None:
    assert stops_cover(stops, D("100"), D("0.05"), 10) is covered


@pytest.mark.parametrize(
    ("rejected_ago", "due"),
    [
        (None, True),  # never rejected
        (timedelta(seconds=5), False),  # the next 5 s sync spends no tag
        (timedelta(seconds=59), False),
        (timedelta(minutes=1), True),
        (timedelta(hours=3), True),
    ],
)
def test_retry_due(rejected_ago: timedelta | None, due: bool) -> None:
    assert retry_due(None if rejected_ago is None else NOW - rejected_ago, NOW) is due


GTT_50 = stop("stop_gtt", qty=50, price=D("990.10"), trigger_price=D("1000.10"))


def sale(**kw: object) -> BrokerOrder:
    fields: dict[str, object] = {
        "order_id": "O-1",
        "tag": None,
        "symbol": "X",
        "side": Side.SELL,
        "qty": 50,
        "filled_qty": 0,
        "status": "placed",
        "product": "CNC",
        "price": D("990.10"),
    }
    fields.update(kw)
    return BrokerOrder(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("order", "seen", "child"),
    [
        (sale(), False, True),  # untagged CNC limit at the GTT's quantity and limit
        (sale(price=None), False, False),  # the owner's market sale on the symbol
        (sale(price=None), True, True),  # HAT made the GTT's sale a market one
        (sale(price=D("1012")), False, False),  # same quantity at another price
        (sale(qty=100), False, False),  # another quantity
        (sale(product="INTRADAY"), False, False),
        (sale(side=Side.BUY), False, False),
        (sale(tag="HTB0ABCDEFGHIJKLMNOP"), False, False),  # an order HAT placed
        (sale(gtt_tag="HTG0ABCDEFGHIJKLMNOP", price=None, qty=7), False, True),  # by its tag
        (sale(gtt_tag="HTG0ABCDEFGHIJKLMNOP", side=Side.BUY), False, False),
    ],
)
def test_gtt_child(order: BrokerOrder, seen: bool, child: bool) -> None:
    assert gtt_child(order, GTT_50, trigger_seen=seen) is child


def young(placed_at: datetime | None) -> bool:
    return placed_at is not None and NOW - placed_at < timedelta(seconds=60)


LOST_DAY = stop(status="unconfirmed")
LOST_DAY_YOUNG = stop(status="unconfirmed", placed_at=NOW - timedelta(seconds=10))
LOST_GTT = stop("stop_gtt", status="unconfirmed")
NEVER_SENT = stop(status="pending", placed_at=None)


@pytest.mark.parametrize(
    ("rows", "open_qty", "exit_live", "after", "step", "reason"),
    [
        ([stop()], 0, False, None, ProtectStep.CLOSED, None),
        ([stop("stop_gtt", "partially_filled")], 100, False, None, ProtectStep.GTT_SELLING, None),
        ([stop()], 100, True, None, ProtectStep.EXIT_WORKING, None),
        ([LOST_GTT], 100, False, None, ProtectStep.SETTLE, None),
        ([stop()], 100, False, None, ProtectStep.COVERED, None),
        ([stop()], 60, False, None, ProtectStep.RESIZE, None),
        ([NEVER_SENT], 100, False, None, ProtectStep.RESEND, None),
        ([LOST_DAY], 100, False, None, ProtectStep.RESEND, None),  # looked up by its tag
        ([LOST_DAY_YOUNG], 100, False, None, ProtectStep.WAIT_LOST, None),  # in flight: no alert
        ([LOST_DAY], 100, False, ProtectStep.RESEND, ProtectStep.WAIT_LOST, "stop_unconfirmed"),
        ([LOST_GTT], 100, False, ProtectStep.SETTLE, ProtectStep.WAIT_LOST, "gtt_unconfirmed"),
        # a GTT its settle left lost may rest: no day stop is sent over it
        (
            [LOST_GTT, LOST_DAY],
            100,
            False,
            ProtectStep.SETTLE,
            ProtectStep.WAIT_LOST,
            "gtt_unconfirmed",
        ),
        ([], 100, False, None, ProtectStep.PLACE_DAY, None),
        ([stop(status="cancelled")], 100, False, None, ProtectStep.PLACE_DAY, None),
    ],
)
def test_protect_plan(
    rows: list[HatOrder],
    open_qty: int,
    exit_live: bool,
    after: ProtectStep | None,
    step: ProtectStep,
    reason: str | None,
) -> None:
    plan = protect_plan(
        rows, open_qty, exit_live=exit_live, day_stop=True, young=young, after=after
    )
    assert (plan.step, plan.reason) == (step, reason)


def test_protect_plan_places_a_gtt_after_the_swap() -> None:
    plan = protect_plan([], 100, exit_live=False, day_stop=False, young=young)
    assert plan.step is ProtectStep.PLACE_GTT


GTT = stop("stop_gtt", trigger_price=D("100"))
OLD_GTT = stop("stop_gtt", placed_at=NOW - timedelta(days=301), trigger_price=D("100"))


@pytest.mark.parametrize(
    ("rows", "goal", "old_series", "after", "step"),
    [
        ([GTT], None, False, None, VerifyStep.NO_TICK),
        ([GTT], D("101"), True, None, VerifyStep.REPLACE_OLD_SERIES),
        ([OLD_GTT], D("101"), False, None, VerifyStep.RENEW),
        ([OLD_GTT], D("99"), False, None, VerifyStep.REFRESH),  # renewed would rest lower
        ([OLD_GTT], D("101"), True, VerifyStep.REPLACE_OLD_SERIES, VerifyStep.RENEW),
        ([OLD_GTT], D("101"), False, VerifyStep.RENEW, VerifyStep.REFRESH),
        ([GTT], D("101"), False, None, VerifyStep.REFRESH),
        ([LOST_GTT], D("101"), False, None, VerifyStep.UNSURE),
        ([], D("101"), False, None, VerifyStep.PLACE),
        ([stop("stop_gtt", "cancelled")], D("101"), False, None, VerifyStep.PLACE),
    ],
)
def test_verify_plan(
    rows: list[HatOrder],
    goal: Decimal | None,
    old_series: bool,
    after: VerifyStep | None,
    step: VerifyStep,
) -> None:
    assert verify_plan(rows, goal, old_series=old_series, wall=NOW, after=after) is step


def test_a_stop_alert_is_raised_once_per_position_reason_and_day() -> None:
    """C22 A4: a stop that cannot be placed or moved is retried at every sync; the owner is told
    once per (code, position, reason) and trade date, again on the next trade date, and again
    at the next sync when the alert could not be delivered."""
    alerted = AlertedToday()
    day, position, other = NOW.date(), uuid.uuid4(), uuid.uuid4()
    assert alerted.first(day, "stop.not_moved", position, "no_tick")
    assert not alerted.first(day, "stop.not_moved", position, "no_tick")
    assert alerted.first(day, "stop.not_moved", position, "broker_unavailable")
    assert alerted.first(day, "stop.not_placed", position, "no_tick")
    assert alerted.first(day, "stop.not_moved", other, "no_tick")
    assert alerted.first(day + timedelta(days=1), "stop.not_moved", position, "no_tick")
    alerted.forget(day, "stop.not_moved", position, "no_tick")  # a stale day forgets nothing
    assert not alerted.first(day + timedelta(days=1), "stop.not_moved", position, "no_tick")
    alerted.forget(day + timedelta(days=1), "stop.not_moved", position, "no_tick")  # not sent
    assert alerted.first(day + timedelta(days=1), "stop.not_moved", position, "no_tick")
