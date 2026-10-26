"""The active broker accounts with their login state for a trade date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configs.records import ACCOUNT_ACTIVE, BrokerAccount
from ..configs.unit import configs_of
from ..models import HatBrokerSession

__all__ = ["MISSING", "SessionRow", "account_label", "broker_sessions"]

MISSING = "missing"  # no `hat_broker_session` row for the day


@dataclass(frozen=True)
class SessionRow:
    account: BrokerAccount
    label: str
    status: str


def account_label(account: BrokerAccount) -> str:
    return f"{account.broker} {account.account_ref}"


def broker_sessions(session: Session, day: date) -> list[SessionRow]:
    """Every active broker account, ordered by id, with its `hat_broker_session` status for
    `day` (`missing` when none is recorded)."""
    accounts = configs_of(session).all(BrokerAccount, lambda a: a.status == ACCOUNT_ACTIVE)
    status: dict[object, str] = dict(
        session.execute(
            select(HatBrokerSession.broker_account_id, HatBrokerSession.status).where(
                HatBrokerSession.broker_account_id.in_([a.id for a in accounts]),
                HatBrokerSession.trade_date == day,
            )
        )
        .tuples()
        .all()
    )
    return [
        SessionRow(account, account_label(account), status.get(account.id, MISSING))
        for account in accounts
    ]
