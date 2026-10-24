"""Broker account onboarding and the capability gate (HAT-BRK-092, HAT-DEC-I8, HAT-DEC-I9,
HAT-EXEC-012).

A broker account becomes usable only after the capability check (funds, positions and holdings
fetched without error) and is recorded in `hat_broker_account` with the adapter's capabilities.
The only adapter registered in the MVP is Fyers. Confirm and auto mode place real orders, so
both need an active account whose capabilities report GTT for delivery (every real fill needs an
overnight broker-side stop) and the owner's confirmation that DDPI is registered
(`confirm_ddpi`, HAT-GOV-085). The gates live in `modes.py` (which imports no broker, so the
engine may read it); the mode helpers are re-exported here. The stored capabilities are a cache:
every session check rewrites them from the adapter (`session.record_session`, HAT-BRK-092), and a
real entry also needs the adapter's own GTT value (`modes.real_orders_allowed`).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.owner import OwnerCommand
from ..errors import HatError
from ..events import record
from ..models import HatBrokerAccount
from ..models.strategy import ACCOUNT_ACTIVE, ACCOUNT_FAILED, ACCOUNT_PENDING, BROKER_FYERS
from ..time import as_utc, trade_date_of
from .broker import (
    CODE_RATE_LIMITED,
    CODE_SESSION_EXPIRED,
    CODE_UNAVAILABLE,
    BrokerError,
    BrokerUnavailableError,
    HatBroker,
)
from .fyers_adapter import build_fyers_broker

__all__ = [
    "ADAPTER_FACTORIES",
    "OnboardingError",
    "onboarding_status",
    "confirm_ddpi",
    "onboard_account",
    "revoke_ddpi",
]


# Every adapter registered in the MVP is the Fyers adapter (HAT-DEC-I8).
ADAPTER_FACTORIES: dict[str, Callable[[], HatBroker]] = {BROKER_FYERS: build_fyers_broker}


class OnboardingError(HatError):
    """The broker is not one the MVP supports."""


_ONBOARD_LOCK = threading.Lock()
DDPI_NOTE_MAX = 200


def require_active_account(
    session: Session, account_id: uuid.UUID, user_id: uuid.UUID
) -> HatBrokerAccount:
    """The user's account, when it passed its capability check (HAT-BRK-092); a missing or
    failed one, or another user's, raises `OnboardingError`, so no sleeve is bound to an account
    that never passed or that is not its owner's (PR 73)."""
    account = session.get(HatBrokerAccount, account_id)
    if account is None or account.user_id != user_id or account.status != ACCOUNT_ACTIVE:
        raise OnboardingError("account_not_active")
    return account


def onboard_account(
    session: Session, broker: HatBroker, *, user_id: uuid.UUID, broker_name: str, account_ref: str
) -> HatBrokerAccount:
    """Run the capability check and record the account: `active` when funds, positions and
    holdings were all fetched, else `failed`, except that a re-run the broker could not answer
    keeps the account's status (`onboarding_status`). Re-running for the same account updates
    it."""
    if broker_name not in ADAPTER_FACTORIES:
        raise OnboardingError("unsupported broker")
    with _ONBOARD_LOCK:  # one lookup-then-insert at a time in this process
        return _onboard(session, broker, user_id, broker_name, account_ref)


def _onboard(
    session: Session, broker: HatBroker, user_id: uuid.UUID, broker_name: str, account_ref: str
) -> HatBrokerAccount:
    account = session.execute(
        select(HatBrokerAccount)
        .where(
            HatBrokerAccount.user_id == user_id,
            HatBrokerAccount.broker == broker_name,
            HatBrokerAccount.account_ref == account_ref,
        )
        # the row is held until the commit, so a concurrent onboarding (another process) waits
        # and its status never overwrites a newer check's
        .with_for_update()
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
    error: BrokerError | None = None
    try:
        broker.funds()
        broker.positions()
        broker.holdings()
    except BrokerError as exc:
        error = exc
    account.capabilities = broker.capabilities().to_json()
    account.status = onboarding_status(account.status, error)
    session.commit()
    return account


# The broker could not answer (an outage, a spent day's requests, an expired token): the check
# says nothing about the account.
_TRANSIENT = frozenset({CODE_UNAVAILABLE, CODE_RATE_LIMITED, CODE_SESSION_EXPIRED})


def onboarding_status(previous: str, error: BrokerError | None) -> str:
    """The account's status after a capability check that raised `error` (None: it passed).
    A check the broker could not answer keeps a status an earlier check set, so one outage
    during a re-run never turns an active account `failed`; a first check that fails, or a
    definite failure (a reply that cannot be read, any other broker error), is `failed`."""
    if error is None:
        return ACCOUNT_ACTIVE
    transient = isinstance(error, BrokerUnavailableError) and error.code in _TRANSIENT
    if transient and previous != ACCOUNT_PENDING:
        return previous
    return ACCOUNT_FAILED


def confirm_ddpi(
    session: Session,
    account_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    since: date,
    note: str,
    command: OwnerCommand,
) -> HatBrokerAccount:
    """Record the owner's word that DDPI (or a POA) is registered on the account's demat since
    `since` (HAT-GOV-085): sets `ddpi_confirmed` with the time and the
    dated note and writes `broker.ddpi_confirmed` (actor user, via cli). A bot reads it as set
    from the next session open on. Refused, writing nothing: an account that is not the user's
    or not active (`account_not_active`), inside market hours or without a calendar row
    (`market_hours`), a date after today (`ddpi_date_in_future`), an empty or overlong note
    (`ddpi_note_required`), or an account already confirmed (`ddpi_already_confirmed`). The
    account row is locked first, so of two confirmations at once the second sees the first."""
    account = session.get(HatBrokerAccount, account_id, with_for_update=True)
    if account is None or account.user_id != user_id or account.status != ACCOUNT_ACTIVE:
        raise OnboardingError("account_not_active")
    now = command.now
    if command.in_session:
        raise OnboardingError("market_hours")
    if since > command.trade_date:
        raise OnboardingError("ddpi_date_in_future")
    text = note.strip()
    if not text or len(text) > DDPI_NOTE_MAX:
        raise OnboardingError("ddpi_note_required")
    if account.ddpi_confirmed:
        raise OnboardingError("ddpi_already_confirmed")
    account.ddpi_confirmed = True
    account.ddpi_confirmed_at = as_utc(now)
    account.ddpi_note = f"{since.isoformat()}: {text}"
    record(
        session,
        aggregate_type="session",
        aggregate_id=account.id,
        event_type="broker.ddpi_confirmed",
        actor="user",
        payload={"chat_id": command.chat_id, "via": "cli", "since": since.isoformat()},
        user_id=account.user_id,
        occurred_at=now,
    )
    session.flush()
    return account


def revoke_ddpi(
    session: Session,
    account_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    note: str,
    now: datetime,
    chat_id: int,
) -> HatBrokerAccount:
    """Withdraw the owner's DDPI confirmation (PR 73, PR 45 follow-up 2): clears
    `ddpi_confirmed` and its time, keeps the dated reason in `ddpi_note` and writes
    `broker.ddpi_revoked` (actor user, via cli). Allowed at any time, inside market hours too: it
    only narrows, and every gate read sees it at once, so the account's bots run shadow from
    their next routing decision (HAT-GOV-085). Refused, writing nothing: an account that is not
    the user's (`account_unknown`), one not confirmed (`ddpi_not_confirmed`), or an empty or
    overlong note (`ddpi_note_required`)."""
    account = session.get(HatBrokerAccount, account_id, with_for_update=True)
    if account is None or account.user_id != user_id:
        raise OnboardingError("account_unknown")
    text = note.strip()
    if not text or len(text) > DDPI_NOTE_MAX:
        raise OnboardingError("ddpi_note_required")
    if not account.ddpi_confirmed:
        raise OnboardingError("ddpi_not_confirmed")
    account.ddpi_confirmed = False
    account.ddpi_confirmed_at = None
    account.ddpi_note = f"revoked {trade_date_of(now).isoformat()}: {text}"
    record(
        session,
        aggregate_type="session",
        aggregate_id=account.id,
        event_type="broker.ddpi_revoked",
        actor="user",
        payload={"chat_id": chat_id, "via": "cli"},
        user_id=account.user_id,
        occurred_at=now,
    )
    session.flush()
    return account
