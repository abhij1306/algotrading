"""Which broker accounts get a desk at start (`daily.accounts.desk_accounts`), and when a failed
reconciliation is an outage to alert (`reconcile_down`)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.hat.daily.accounts import RECONCILE_DOWN_AFTER, desk_accounts, reconcile_down
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
    assert reconcile_down(False, None, NOW)
    assert reconcile_down(False, NOW - timedelta(days=1), NOW)


def test_a_failure_after_a_success_is_down_only_once_it_is_old() -> None:
    """A routine re-run failing just after a success is not an outage; one still failing
    `RECONCILE_DOWN_AFTER` after the last success is (C22 A1)."""
    assert not reconcile_down(True, NOW - timedelta(minutes=5), NOW)
    assert not reconcile_down(True, NOW - RECONCILE_DOWN_AFTER + timedelta(seconds=1), NOW)
    assert reconcile_down(True, NOW - RECONCILE_DOWN_AFTER, NOW)
