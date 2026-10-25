"""Which broker accounts get a desk at start (`daily.accounts.desk_accounts`), and when a failed
reconciliation is an outage to alert (`reconcile_down`)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.hat.daily.accounts import (
    RECONCILE_DOWN_AFTER,
    RECONCILE_DOWN_IN_SESSION,
    desk_accounts,
    reconcile_down,
)
from app.hat.execution.guard import BUDGET_SPENT, ExecutionGuard
from app.hat.models import HatBrokerAccount


def _account(status: str, broker: str = "fyers") -> HatBrokerAccount:
    return HatBrokerAccount(id=uuid.uuid4(), broker=broker, status=status)


def test_a_holding_account_sharing_an_active_broker_gets_no_desk() -> None:
    failed, active = _account("failed"), _account("active")
    assert desk_accounts([failed, active]) == [active]


def test_a_holding_account_alone_on_its_broker_gets_a_desk() -> None:
    active, failed = _account("active"), _account("failed", broker="other")
    assert desk_accounts([failed, active]) == [active, failed]


def test_two_holding_accounts_on_one_broker_get_one_desk() -> None:
    first, second = _account("failed"), _account("pending")
    assert desk_accounts([first, second]) == [first]


def test_two_active_accounts_on_one_broker_both_reach_broker_for() -> None:
    """`broker_for` refuses the start for these (HAT-OPS-002); the filter does not hide them."""
    one, two = _account("active"), _account("active")
    assert desk_accounts([one, two]) == [one, two]


NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


def test_a_failure_before_the_days_first_success_is_down() -> None:
    for in_session in (True, False):
        assert reconcile_down(False, None, NOW, in_session=in_session)
        assert reconcile_down(False, NOW - timedelta(days=1), NOW, in_session=in_session)


def test_a_failure_after_a_success_is_down_after_10_minutes_in_session_30_outside() -> None:
    """A routine re-run failing just after a success is not an outage; one still failing 10
    minutes after the last success is during the session, 30 minutes outside it (C22 A1)."""
    assert RECONCILE_DOWN_IN_SESSION == timedelta(minutes=10)
    assert RECONCILE_DOWN_AFTER == timedelta(minutes=30)
    second = timedelta(seconds=1)
    assert not reconcile_down(True, NOW - timedelta(minutes=10) + second, NOW, in_session=True)
    assert reconcile_down(True, NOW - timedelta(minutes=10), NOW, in_session=True)
    assert not reconcile_down(True, NOW - timedelta(minutes=10), NOW, in_session=False)
    assert not reconcile_down(True, NOW - timedelta(minutes=30) + second, NOW, in_session=False)
    assert reconcile_down(True, NOW - timedelta(minutes=30), NOW, in_session=False)


def test_a_spent_day_refuses_entries_never_stops_or_sales() -> None:
    """Q11: once the broker calls of the day are spent an entry is refused; a stop or a sale
    never is."""
    spent = ExecutionGuard(today=lambda: NOW.date(), budget_spent=lambda: True)
    spent.reconciled_for = NOW.date()
    bot, account = uuid.uuid4(), uuid.uuid4()
    assert spent.refusal(bot, account, is_entry=True) == BUDGET_SPENT
    assert spent.refusal(bot, account, is_entry=False) is None
    spent.budget_spent = lambda: False
    assert spent.refusal(bot, account, is_entry=True) is None
