"""Plain table tests for the pure rules on the order path: the auto route (D149), the GTT gate
at placement (HAT-EXEC-012), the retry of an order of unknown outcome (HAT-BRK-102), the entry
cancel and the broker's held quantities read by reconciliation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.hat.execution.broker import BrokerHolding, BrokerPosition
from app.hat.execution.entry_timeout import BREAK_FAILED, TIMED_OUT, cancel_reason
from app.hat.execution.modes import HIGH, real_orders_allowed, route_mode
from app.hat.execution.order_book import ENTRY, GTT_PURPOSE
from app.hat.execution.reconcile import broker_quantities
from app.hat.execution.submit import Retry, retry_verdict
from app.hat.governance import Mode
from app.hat.models.strategy import ACCOUNT_ACTIVE

D = Decimal
NOW = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("mode", "confidence", "route"),
    [
        (Mode.AUTO, HIGH, Mode.AUTO),
        (Mode.AUTO, "medium", Mode.CONFIRM),
        (Mode.AUTO, "low", Mode.CONFIRM),
        (Mode.AUTO, None, Mode.CONFIRM),
        (Mode.AUTO, "HIGH", Mode.CONFIRM),
        (Mode.CONFIRM, HIGH, Mode.CONFIRM),
        (Mode.CONFIRM, "low", Mode.CONFIRM),
        (Mode.SHADOW, HIGH, Mode.SHADOW),
        (Mode.SHADOW, None, Mode.SHADOW),
    ],
)
def test_route_mode(mode: str, confidence: str | None, route: str) -> None:
    """Auto places only a signal labelled exactly high; anything else goes to the owner."""
    assert route_mode(mode, confidence) == route


@pytest.mark.parametrize(
    ("status", "capabilities", "reported", "allowed"),
    [
        (ACCOUNT_ACTIVE, {"gtt_delivery": True}, True, True),
        (ACCOUNT_ACTIVE, {"gtt_delivery": True}, False, False),  # the stored cache only narrows
        (ACCOUNT_ACTIVE, {"gtt_delivery": False}, True, False),
        (ACCOUNT_ACTIVE, {"gtt_delivery": "true"}, True, False),
        (ACCOUNT_ACTIVE, {}, True, False),
        ("pending", {"gtt_delivery": True}, True, False),
        (ACCOUNT_ACTIVE, {"gtt_delivery": True}, 1, False),
    ],
)
def test_real_orders_allowed(
    status: str, capabilities: dict, reported: object, allowed: bool
) -> None:
    assert real_orders_allowed(status, capabilities, reported) is allowed  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("purpose", "filled", "verdict"),
    [
        (ENTRY, None, Retry.NEVER),  # the book may lag an entry that landed
        (ENTRY, 0, Retry.ADOPT),
        (ENTRY, 40, Retry.ADOPT),
        (GTT_PURPOSE, None, Retry.NEVER),
        (GTT_PURPOSE, 0, Retry.NEVER),
        ("stop_day", None, Retry.SEND),
        ("stop_day", 0, Retry.ADOPT),
        ("stop_day", 10, Retry.AWAIT_FILLS),
        ("exit", None, Retry.SEND),
        ("exit", 0, Retry.ADOPT),
        ("exit", 1, Retry.AWAIT_FILLS),
    ],
)
def test_retry_verdict(purpose: str, filled: int | None, verdict: Retry) -> None:
    """An entry or a GTT of unknown outcome is never sent again (HAT-BRK-102); a stop or sale the
    book does not hold is; one found with shares sold waits for the fill sync."""
    assert retry_verdict(purpose, filled) is verdict


TRIGGERED = NOW - timedelta(minutes=30)
PLACED = TRIGGERED + timedelta(seconds=5)
TIMEOUT = timedelta(minutes=60)


@pytest.mark.parametrize(
    ("closes", "now", "reason"),
    [
        ([], NOW, None),
        ([(TRIGGERED, D("99"))], NOW, None),  # the trigger bar itself never counts
        ([(TRIGGERED + timedelta(minutes=5), D("100"))], NOW, None),  # at the level holds
        ([(TRIGGERED + timedelta(minutes=5), D("99.95"))], NOW, BREAK_FAILED),
        ([], PLACED + TIMEOUT - timedelta(seconds=1), None),
        ([], PLACED + TIMEOUT, TIMED_OUT),
        ([(TRIGGERED + timedelta(minutes=5), D("99"))], PLACED + TIMEOUT, BREAK_FAILED),
    ],
)
def test_cancel_reason(closes: list, now: datetime, reason: str | None) -> None:
    """A final bar after the trigger bar closing below the level fails the break; else the entry
    times out exactly `timeout` after it was placed."""
    got = cancel_reason(
        level=D("100"),
        triggered_at=TRIGGERED,
        placed_at=PLACED,
        closes=closes,
        now=now,
        timeout=TIMEOUT,
    )
    assert got == reason


def test_broker_quantities() -> None:
    """The holding plus today's signed net CNC position; intraday products are ignored."""
    positions = [
        BrokerPosition("NSE:A-EQ", 10, D(100), "CNC"),
        BrokerPosition("NSE:B-EQ", -4, D(50), "CNC"),
        BrokerPosition("NSE:A-EQ", 99, D(100), "INTRADAY"),
        BrokerPosition("NSE:C-EQ", 7, D(10), "MARGIN"),
    ]
    holdings = [BrokerHolding("NSE:A-EQ", 5, D(90)), BrokerHolding("NSE:B-EQ", 4, D(40))]
    assert broker_quantities(positions, holdings) == {"NSE:A-EQ": 15, "NSE:B-EQ": 0}
    assert broker_quantities([], []) == {}
