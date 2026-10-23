"""Plain table tests for the pure stop planners (`execution/stop_plans.py`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.hat.execution.stop_plans import (
    ProtectStep,
    VerifyStep,
    protect_plan,
    raise_only,
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
