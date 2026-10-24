"""Which broker accounts get a desk at start (`daily.accounts.desk_accounts`)."""

import uuid

from app.hat.daily.accounts import desk_accounts
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
