"""Broker session ownership and the morning login record (HAT-DEC-T8, HAT-BRK-082, HAT-OPS-031;
07 §8; 10 §4).

One service owns each broker account's session and serialises its broker calls
(`SessionRegistry.acquire`); a second owner for a live account is refused. `record_session` keeps
`hat_broker_session` current from the adapter's status, and `SessionWatch` decides the retry
backoff and the one `broker.session_lost` alert per outage, which is due once the outage lasts 2
minutes. Backoff: exponential from 1 s, capped at 30 s [OWNER-PENDING default].
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypeVar

from sqlalchemy.orm import Session

from ..errors import HatError
from ..events import record
from ..models import HatBrokerAccount, HatBrokerSession
from ..time import utc_now
from .broker import HatBroker, SessionStatus

__all__ = [
    "ALERT_AFTER",
    "BACKOFF_CAP_SECONDS",
    "SessionOwner",
    "SessionOwnedError",
    "SessionRegistry",
    "SessionWatch",
    "backoff_delay",
    "record_session",
]

ALERT_AFTER = timedelta(minutes=2)
BACKOFF_CAP_SECONDS = 30
T = TypeVar("T")


class SessionOwnedError(HatError):
    """The account's session already has an owner."""


class SessionOwner:
    """The one owner of an account's session: every broker call goes through `call`, one at a
    time."""

    def __init__(self, registry: SessionRegistry, account_id: uuid.UUID) -> None:
        self._registry = registry
        self.account_id = account_id
        self._calls = threading.Lock()
        self._released = False

    def call(self, fn: Callable[[], T]) -> T:
        if self._released:
            raise SessionOwnedError("this session owner was released")
        with self._calls:
            if self._released:
                raise SessionOwnedError("this session owner was released")
            return fn()

    def release(self) -> None:
        """Wait for the call in flight (queued calls then refuse), then give the session up."""
        with self._calls:
            self._released = True
        self._registry.release(self)


class SessionRegistry:
    """Hands out at most one `SessionOwner` per broker account."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[uuid.UUID, SessionOwner] = {}

    def acquire(self, account_id: uuid.UUID) -> SessionOwner:
        with self._lock:
            if account_id in self._owners:
                raise SessionOwnedError("the broker session already has an owner")
            owner = SessionOwner(self, account_id)
            self._owners[account_id] = owner
            return owner

    def release(self, owner: SessionOwner) -> None:
        with self._lock:
            if self._owners.get(owner.account_id) is owner:
                del self._owners[owner.account_id]


def backoff_delay(attempt: int) -> int:
    """Seconds to wait before retry number `attempt` (0, 1, ...): 1, 2, 4, ... capped at 30."""
    return min(BACKOFF_CAP_SECONDS, 1 << min(max(attempt, 0), 16))


@dataclass(frozen=True)
class WatchStep:
    retry_in: int | None  # seconds until the next retry while the session is down
    alert: bool  # the one session-lost alert of this outage is due now


class SessionWatch:
    """Tracks one account's outages: feed it each status check in time order."""

    def __init__(self) -> None:
        self._down_since: datetime | None = None
        self._attempt = 0
        self._alerted = False

    def observe(self, now: datetime, live: bool) -> WatchStep:
        if live:
            self._down_since, self._attempt, self._alerted = None, 0, False
            return WatchStep(None, False)
        if self._down_since is None:
            self._down_since = now
        alert = not self._alerted and now - self._down_since >= ALERT_AFTER
        self._alerted = self._alerted or alert
        delay = backoff_delay(self._attempt)
        self._attempt += 1
        return WatchStep(delay, alert)


def record_session(
    session: Session,
    broker: HatBroker,
    account_id: uuid.UUID,
    trade_date: date,
    *,
    lost_alert: bool = False,
    now: datetime | None = None,
    status: SessionStatus | None = None,
) -> SessionStatus:
    """Write today's `hat_broker_session` row from the adapter's status (`status` when the
    caller has just read it, so the row and the caller's `SessionWatch` see one read) and
    commit. A first login of the day records `broker.logged_in`; `lost_alert` (from
    `SessionWatch`) records `broker.session_lost`."""
    if status is None:
        status = broker.session_status()
    instant = now or utc_now()
    row = session.get(HatBrokerSession, (account_id, trade_date))
    first_login = status is SessionStatus.LIVE and (row is None or row.logged_in_at is None)
    if row is None:
        row = HatBrokerSession(broker_account_id=account_id, trade_date=trade_date, status="")
        session.add(row)
    row.status = status.value
    if first_login:
        row.logged_in_at = instant
    if first_login:
        record(
            session,
            aggregate_type="session",
            aggregate_id=account_id,
            event_type="broker.logged_in",
            actor="broker",
            payload={"status": status.value},
        )
    if status is SessionStatus.LIVE:
        _refresh_capabilities(session, broker, account_id)
    if lost_alert:
        record(
            session,
            aggregate_type="session",
            aggregate_id=account_id,
            event_type="broker.session_lost",
            actor="broker",
            payload={"status": status.value},
        )
    session.commit()
    return status


def _refresh_capabilities(session: Session, broker: HatBroker, account_id: uuid.UUID) -> None:
    """HAT-BRK-092: the stored capabilities are the adapter's, rewritten at every live session
    check, so a flip of the adapter's value reaches the account by the next check, and a change
    is recorded as `broker.capabilities_changed` (no commit; the caller commits)."""
    account = session.get(HatBrokerAccount, account_id)
    if account is None:
        return
    current = broker.capabilities().to_json()
    previous = account.capabilities or {}
    if previous == current:
        return
    account.capabilities = current
    record(
        session,
        aggregate_type="session",
        aggregate_id=account_id,
        event_type="broker.capabilities_changed",
        actor="broker",
        payload={
            "gtt_delivery": current.get("gtt_delivery"),
            "was_gtt_delivery": previous.get("gtt_delivery"),
        },
    )
