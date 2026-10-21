"""Broker account onboarding and the capability gate (HAT-BRK-092, HAT-DEC-I8, HAT-DEC-I9,
HAT-EXEC-012; 07 §8-§9).

A broker account becomes usable only after the capability check (funds, positions and holdings
fetched without error) and is recorded in `hat_broker_account` with the adapter's capabilities.
The only adapter registered in the MVP is Fyers. Confirm and auto mode place real orders, and
every real fill needs an overnight broker-side stop, so both need an active account whose
capabilities report GTT for delivery; the Fyers adapter reports none until the owner verifies GTT
on a live account (PLAN Q8), so the MVP runs shadow only until then.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatBrokerAccount
from .broker import BrokerError, HatBroker
from .fyers_adapter import build_fyers_broker
from .submit import ACCOUNT_ACTIVE

__all__ = [
    "ACCOUNT_FAILED",
    "ACCOUNT_PENDING",
    "ADAPTER_FACTORIES",
    "MODES",
    "ModeRefusedError",
    "OnboardingError",
    "effective_mode",
    "onboard_account",
    "request_mode",
]

ACCOUNT_PENDING = "pending"
ACCOUNT_FAILED = "failed"
MODES = ("shadow", "confirm", "auto")

# Every adapter registered in the MVP is the Fyers adapter (HAT-DEC-I8).
ADAPTER_FACTORIES: dict[str, Callable[[], HatBroker]] = {"fyers": build_fyers_broker}


class OnboardingError(HatError):
    """The broker is not one the MVP supports."""


class ModeRefusedError(HatError):
    """A mode change the account's capabilities do not allow."""


def onboard_account(
    session: Session, broker: HatBroker, *, user_id: uuid.UUID, broker_name: str, account_ref: str
) -> HatBrokerAccount:
    """Run the capability check and record the account: `active` when funds, positions and
    holdings were all fetched, else `failed`. Re-running for the same account updates it."""
    if broker_name not in ADAPTER_FACTORIES:
        raise OnboardingError("unsupported broker")
    account = session.execute(
        select(HatBrokerAccount).where(
            HatBrokerAccount.user_id == user_id,
            HatBrokerAccount.broker == broker_name,
            HatBrokerAccount.account_ref == account_ref,
        )
    ).scalar_one_or_none()
    if account is None:
        account = HatBrokerAccount(
            user_id=user_id,
            broker=broker_name,
            account_ref=account_ref,
            capabilities={},
            status=ACCOUNT_PENDING,
        )
        session.add(account)
    try:
        broker.funds()
        broker.positions()
        broker.holdings()
        passed = True
    except BrokerError:
        passed = False
    account.capabilities = broker.capabilities().to_json()
    account.status = ACCOUNT_ACTIVE if passed else ACCOUNT_FAILED
    session.commit()
    return account


def _gtt_ready(account_status: str, capabilities: Mapping[str, object]) -> bool:
    return account_status == ACCOUNT_ACTIVE and capabilities.get("gtt_delivery") is True


def effective_mode(
    requested: str, *, account_status: str, capabilities: Mapping[str, object]
) -> str:
    """The mode a bot runs in: confirm and auto fall back to shadow unless the account is
    active and reports GTT for delivery."""
    if requested not in MODES:
        return "shadow"
    if requested != "shadow" and not _gtt_ready(account_status, capabilities):
        return "shadow"
    return requested


def request_mode(requested: str, *, account_status: str, capabilities: Mapping[str, object]) -> str:
    """Accept a mode request or refuse it (ModeRefusedError) when the account cannot run it."""
    if (
        effective_mode(requested, account_status=account_status, capabilities=capabilities)
        != requested
    ):
        raise ModeRefusedError("mode needs an active account with GTT for delivery")
    return requested
