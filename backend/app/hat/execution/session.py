"""Broker session ownership and the morning login record (HAT-DEC-T8, HAT-BRK-082, HAT-OPS-031).

One service owns each broker account's session and serialises its broker calls
(`SessionRegistry.acquire`); a second owner for a live account is refused. `record_session` keeps
`hat_broker_session` current from the adapter's status, and `SessionWatch` decides the retry
backoff and the one `broker.session_lost` alert per outage, which is due once the outage lasts 2
minutes. Backoff: exponential from 1 s, capped at 30 s [OWNER-PENDING default].

The token expires at midnight IST and the owner logs in after the 08:30 prompt, so a session that
is not live before 08:30 is not an outage: the watch counts outages from 08:30 (HAT-OPS-031), and
`broker_health_failing` says when a session that is not live trips the bots' `broker_health`
kill, from 09:05 (the repeat prompt) until the 15:30 close (HAT-RISK-063, design review item 5).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TypeVar

from sqlalchemy.orm import Session

from ..configs.records import BrokerAccount
from ..configs.unit import configs_of
from ..errors import HatError
from ..events import record
from ..models import HatBrokerSession
from ..time import IST, utc_now
from .broker import HatBroker, SessionStatus

__all__ = [
    "ALERT_AFTER",
    "BACKOFF_CAP_SECONDS",
    "BROKER_HEALTH_FROM",
    "BROKER_HEALTH_UNTIL",
    "WATCH_FROM",
    "SessionOwner",
    "SessionOwnedError",
    "SessionRegistry",
    "SessionWatch",
    "backoff_delay",
    "broker_health_failing",
    "record_session",
    "refresh_capabilities",
]

logger = logging.getLogger(__name__)

ALERT_AFTER = timedelta(minutes=2)
BACKOFF_CAP_SECONDS = 30
WATCH_FROM = time(8, 30)  # IST: the login prompt; a logged-out session before it is no outage
BROKER_HEALTH_FROM = time(9, 5)  # IST: the repeat prompt
BROKER_HEALTH_UNTIL = time(15, 30)  # IST: the close
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


def broker_health_failing(now: datetime, live: bool) -> bool:
    """True when a session check at `now` trips `broker_health` (HAT-RISK-063): the session is
    not live between 09:05 and the 15:30 close IST. Any other check clears it."""
    at = now.astimezone(IST).time()
    return not live and BROKER_HEALTH_FROM <= at < BROKER_HEALTH_UNTIL


class SessionWatch:
    """Tracks one account's outages: feed it each status check in time order. An outage starts
    no earlier than 08:30 IST (`WATCH_FROM`): before it, a session that is not live is the night
    between the midnight expiry and the morning login, and resets the watch."""

    def __init__(self) -> None:
        self._down_since: datetime | None = None
        self._attempt = 0
        self._alerted = False

    def observe(self, now: datetime, live: bool) -> WatchStep:
        if live or now.astimezone(IST).time() < WATCH_FROM:
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
    refresh_capabilities(session, broker, account_id)  # after the row: a failure never undoes it
    return status


def refresh_capabilities(session: Session, broker: HatBroker, account_id: uuid.UUID) -> bool:
    """HAT-BRK-092: rewrite the account's stored capabilities from the adapter's (a local value,
    no broker call) and commit; a change records `broker.capabilities_changed`, which the relay
    sends as a health alert. True when they changed. A failure is logged and rolled back alone,
    so it never undoes the caller's session row."""
    try:
        account = configs_of(session).get(BrokerAccount, account_id)
        if account is None:
            return False
        current = broker.capabilities().to_json()
        previous = account.capabilities or {}
        if previous == current:
            return False
        account.capabilities = current
        record(
            session,
            aggregate_type="session",
            aggregate_id=account_id,
            event_type="broker.capabilities_changed",
            actor="broker",
            payload={
                "gtt_delivery": _flag(current.get("gtt_delivery")),
                "was_gtt_delivery": _flag(previous.get("gtt_delivery")),
                "changed": sorted(
                    k for k in current.keys() | previous.keys() if current.get(k) != previous.get(k)
                ),
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("capabilities of account %s not refreshed", account_id)
        return False
    return True


def _flag(value: object) -> str:
    return "true" if value is True else "false"
