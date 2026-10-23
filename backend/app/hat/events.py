"""The event store: `record()` appends to `hat_event` in the caller's transaction.

`record` never commits and never opens a session of its own: the event is written by the same
database transaction as the state change it describes, so the two commit or roll back together
(HAT-SCH-062, HAT-DEC-I15). It accepts only the 35 event types of the MVP catalogue
(HAT-SCH-063), so a typo cannot create an event no replay or discipline metric reads.

Event ids: Postgres assigns the `bigint` identity. SQLite cannot give a composite primary key an
identity, so there `record` assigns `max(id) + 1`, never below an id this process already handed
out for that database (a per-database counter under a lock). Ids are unique and strictly
increase in call order within a process; a rolled-back event leaves a gap, as an identity does
(HAT-SCH-067).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.hat.clock import require_aware

from .errors import HatError
from .models import HatEvent, HatOrder, HatPosition
from .models.events import EVENT_ACTORS
from .settings import load_settings
from .time import utc_now

__all__ = [
    "ACTORS",
    "Actor",
    "AGGREGATE_TYPES",
    "EVENT_GROUPS",
    "EVENT_TYPES",
    "TRADE_GROUPS",
    "EventError",
    "record",
]

# Spec verbatim (the Portfolio group is PR 9's).
# `CONF events.catalogue` compares this with the spec.
EVENT_GROUPS: dict[str, tuple[str, ...]] = {
    "Data": ("ingest.completed", "ingest.failed", "quality.failed", "gapscan.completed"),
    "Signals": (
        "watchlist.published",
        "signal.triggered",
        "signal.rejected",
        "signal.approved",
        "signal.skipped",
        "signal.expired",
    ),
    "Orders": (
        "order.placed",
        "order.rejected",
        "order.filled",
        "order.cancelled",
        "gtt.placed",
        "gtt.refreshed",
        "gtt.triggered",
    ),
    "Positions": (
        "position.opened",
        "stop.moved",
        "exit.partial",
        "position.closed",
        "exit.close_evaluated",
        "exit.close_gap",
        "position.rescaled",
        "position.qty_corrected",
        "corporate_action.unhandled",
        "corporate_action.resolved",
    ),
    "Control": (
        "mode.change_requested",
        "mode.applied",
        "mode.apply_refused",
        "bot.paused",
        "bot.resumed",
        "kill.tripped",
        "kill.rearmed",
        "flatten.executed",
    ),
    "Governance": (
        "strategy.version_created",
        "strategy.promoted",
        "risk.change_requested",
        "risk.change_effective",
    ),
    "Portfolio": (
        "sleeve.lifecycle_changed",
        "sleeve.version_assigned",
        "regime.state_recorded",
        "allocation.computed",
    ),
    "Sessions": (
        "broker.logged_in",
        "broker.session_lost",
        "broker.capabilities_changed",
        "broker.ddpi_confirmed",
        "broker.ddpi_revoked",
        "feed.stale",
    ),
}
EVENT_TYPES: frozenset[str] = frozenset(t for group in EVENT_GROUPS.values() for t in group)

AGGREGATE_TYPES: frozenset[str] = frozenset(
    {
        "signal",
        "order",
        "position",
        "strategy_version",
        "risk_policy",
        "bot",
        "regime",
        "session",
        "ingest",
    }
)
ACTORS: frozenset[str] = frozenset(EVENT_ACTORS)
# Who caused an event. A `user` event is a Telegram command or button and carries the sender's
# `chat_id` in its payload (HAT-SCH-065, HAT-TG-013): `record()` refuses one without it.
Actor = Literal["engine", "user", "broker", "scheduler"]

# Events of these groups belong to one signal's trade, so they carry the signal id as their
# correlation id. `watchlist.published` is the exception: a watchlist belongs to a bot and a
# day, not to one signal (a finding recorded in the PR).
TRADE_GROUPS = ("Signals", "Orders", "Positions")
NOT_TRADE_EVENTS = frozenset({"watchlist.published"})
TRADE_EVENT_TYPES: frozenset[str] = frozenset(
    t for group in TRADE_GROUPS for t in EVENT_GROUPS[group] if t not in NOT_TRADE_EVENTS
)

_sqlite_lock = threading.Lock()
_sqlite_last_id: dict[str, int] = {}


class EventError(HatError):
    """The event was refused before anything was written. The message names the rule, never
    the payload."""


def _check(
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
    correlation_id: uuid.UUID | None,
) -> None:
    if event_type not in EVENT_TYPES:
        raise EventError("unknown event type")
    if aggregate_type not in AGGREGATE_TYPES:
        raise EventError("unknown aggregate type")
    if actor not in ACTORS:
        raise EventError("unknown actor")
    if event_type in TRADE_EVENT_TYPES:
        if correlation_id is None:
            raise EventError("a trade event needs the signal id as correlation_id")
        if aggregate_type == "signal" and correlation_id != aggregate_id:
            raise EventError("a signal event's correlation_id is the signal's own id")
    chat_id = payload.get("chat_id")
    if actor == "user" and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
        raise EventError("a user event carries the Telegram chat_id in its payload")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EventError("payload is not JSON serializable") from exc


_LINKED_ROWS = {"order": HatOrder, "position": HatPosition}


def _check_linked_signal(
    session: Session,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    correlation_id: uuid.UUID | None,
) -> None:
    """HAT-SCH-064: a trade event about an order or a position carries the id of the signal that
    row belongs to; an unknown row, or another signal's id, is refused."""
    model = _LINKED_ROWS.get(aggregate_type)
    if model is None or event_type not in TRADE_EVENT_TYPES:
        return
    linked = session.scalar(select(model.signal_id).where(model.id == aggregate_id))
    if linked is None or linked != correlation_id:
        raise EventError("an order or position event's correlation_id is its own signal's id")


def _next_sqlite_id(session: Session) -> int:
    bind = session.get_bind()
    key = str(bind.engine.url)
    stored = session.execute(select(func.coalesce(func.max(HatEvent.id), 0))).scalar_one()
    with _sqlite_lock:
        value = max(stored, _sqlite_last_id.get(key, 0)) + 1
        _sqlite_last_id[key] = value
    return value


def record(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    actor: Actor,
    payload: Mapping[str, Any],
    correlation_id: uuid.UUID | None = None,
    causation_id: int | None = None,
    bot_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> int:
    """Append one event in `session`'s open transaction and return its id.

    Nothing is committed: the caller's commit (or rollback) decides the event together with the
    state change it describes. Raises `EventError`, writing nothing, when the type, aggregate
    type or actor is outside the catalogue, a trade event has no `correlation_id` (or, about an
    order or a position, not the id of that row's signal), a user event
    has no `chat_id`, the payload is not JSON, or `causation_id` names no earlier event.
    """
    _check(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor=actor,
        payload=payload,
        correlation_id=correlation_id,
    )
    _check_linked_signal(session, aggregate_type, aggregate_id, event_type, correlation_id)
    if causation_id is not None:
        found = session.execute(select(HatEvent.id).where(HatEvent.id == causation_id)).first()
        if found is None:
            raise EventError("causation_id names no earlier event")
    if occurred_at is not None:
        require_aware(occurred_at)
    now = utc_now()
    values: dict[str, Any] = {
        "recorded_at": now,
        "occurred_at": occurred_at or now,
        "user_id": user_id or load_settings().user_id,
        "bot_id": bot_id,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
        "actor": actor,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "payload": dict(payload),
    }
    if session.get_bind().dialect.name == "sqlite":
        values["id"] = _next_sqlite_id(session)
        session.execute(insert(HatEvent).values(**values))
        return values["id"]
    result = session.execute(insert(HatEvent).values(**values).returning(HatEvent.id))
    return result.scalar_one()
