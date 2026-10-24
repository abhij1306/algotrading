"""The active broker accounts with their login state for a trade date, read in one query."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatBrokerAccount, HatBrokerSession
from ..models.strategy import ACCOUNT_ACTIVE

__all__ = ["MISSING", "SessionRow", "account_label", "broker_sessions"]

MISSING = "missing"  # no `hat_broker_session` row for the day


@dataclass(frozen=True)
class SessionRow:
    account: HatBrokerAccount
    label: str
    status: str


def account_label(account: HatBrokerAccount) -> str:
    return f"{account.broker} {account.account_ref}"


def broker_sessions(session: Session, day: date) -> list[SessionRow]:
    """Every active broker account, ordered by id, with its `hat_broker_session` status for
    `day` (`missing` when none is recorded)."""
    rows = session.execute(
        select(HatBrokerAccount, HatBrokerSession.status)
        .outerjoin(
            HatBrokerSession,
            (HatBrokerSession.broker_account_id == HatBrokerAccount.id)
            & (HatBrokerSession.trade_date == day),
        )
        .where(HatBrokerAccount.status == ACCOUNT_ACTIVE)
        .order_by(HatBrokerAccount.id)
    )
    return [
        SessionRow(account, account_label(account), status or MISSING) for account, status in rows
    ]
