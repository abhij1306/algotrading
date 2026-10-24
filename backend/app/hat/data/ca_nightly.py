"""The nightly dividend import (CA4): `ca.adjust` reads NSE's daily corporate-action file
(`ca_bc.read_bc`) and runs its dividends up to the next session through the same proposals,
checks and verdicts as the feed (`ca_payouts`; table `nightly`, source `nse_pr_bc`): a review
case is alerted, never guessed, and a stored dividend without an amount (the MCP lookup stores
none) gets the file's. `missing_amounts` lists the dividends still without one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatInstrument
from .ca_bc import SOURCE_BC, FeedRow, read_bc
from .ca_gate import CONFLICT, REVIEW
from .ca_payouts import T_NIGHTLY, Outcome, apply, evaluate
from .corporate_actions import TYPE_DIVIDEND
from .store import DataStore

# NSE lists a dividend in its daily file from a few days before its ex-date to the ex-date's own
# file, the last word on a revised amount; one still listed after it is read for this long.
CATCH_UP_DAYS = 7
ALERT_DIVIDEND_REVIEW = "dividend.review"  # a listed dividend is a review or conflict case
ALERT_DIVIDEND_NO_AMOUNT = "dividend.amount_missing"  # a stored dividend has no cash amount


@dataclass(frozen=True)
class MissingAmount:
    """A stored dividend without a cash amount: price-neutral in the total-return series."""

    symbol: str
    isin: str
    ex_date: date
    source: str


def nightly_window(rows: Iterable[FeedRow], day: date) -> list[FeedRow]:
    """The rows of the night's file whose ex-date is `day` or up to `CATCH_UP_DAYS` before it
    (a dividend is stored on its ex-date's night, from that day's file), and every row whose
    ex-date cannot be read: a dividend among them is a `bad_ex_date` review case, alerted."""
    first = day - timedelta(days=CATCH_UP_DAYS)
    return [r for r in rows if r.ex_date is None or first <= r.ex_date <= day]


def missing_amounts(
    session: Session, since: date | None = None, through: date | None = None
) -> list[MissingAmount]:
    """Every stored dividend without a cash amount (ex-dates in `since..through` when given),
    newest first: each is a drop the total-return series does not pay back."""
    query = (
        select(HatInstrument.symbol, HatInstrument.isin, HatCorporateAction)
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(
            HatCorporateAction.action_type == TYPE_DIVIDEND,
            HatCorporateAction.cash_amount.is_(None),
        )
        .order_by(HatCorporateAction.ex_date.desc(), HatInstrument.symbol)
    )
    if since is not None:
        query = query.where(HatCorporateAction.ex_date >= since)
    if through is not None:
        query = query.where(HatCorporateAction.ex_date <= through)
    return [
        MissingAmount(symbol, isin, row.ex_date, row.source)
        for symbol, isin, row in session.execute(query)
    ]


@dataclass
class NightlyReport:
    outcomes: list[Outcome] = field(default_factory=list)
    created: int = 0
    added: int = 0
    missing: list[MissingAmount] = field(default_factory=list)

    @property
    def flagged(self) -> list[Outcome]:
        """The review and conflict cases: alerted, never stored."""
        return [o for o in self.outcomes if o.status in (REVIEW, CONFLICT)]


def bc_journal(text: str, day: date) -> dict[str, Any]:
    """What each row stored from a `bc` file journals of it."""
    return {
        "source": SOURCE_BC,
        "day": day.isoformat(),
        "bc_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def nightly_import(
    session: Session, store: DataStore, text: str, day: date, now: datetime
) -> NightlyReport:
    """Store the dividends the night's `bc` file lists for `day` (and the catch-up days) as
    the one-off import stores the feed's (`evaluate`, `apply`: the same checks and verdicts, table
    `nightly`, source `nse_pr_bc`), in the caller's transaction, and list the stored dividends
    of those days still without an amount. Raises `CaFileError` for a file it cannot read."""
    rows = nightly_window(read_bc(text), day)
    report = NightlyReport()
    if rows:
        report.outcomes = evaluate(session, store, rows, (), T_NIGHTLY)
        journal = bc_journal(text, day)
        report.created, report.added = apply(session, report.outcomes, journal=journal, now=now)
    report.missing = missing_amounts(session, day - timedelta(days=CATCH_UP_DAYS), day)
    return report
