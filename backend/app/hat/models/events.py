"""`hat_event`: the append-only event log.

On Postgres the table is range-partitioned monthly on `recorded_at` and the trigger
`hat_event_no_update` (`app/hat/schema.py`) rejects every UPDATE and DELETE. The primary key is
(id, recorded_at) because a partitioned table's key must contain the partition column.

On SQLite (tests only) a composite key cannot carry an identity, so `app/hat/events.py`
assigns the ids there (HAT-SCH-067).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    func,
)

from ...base import Base
from ._types import JsonB, Timestamptz, now_utc

__all__ = ["EVENT_ACTORS", "HatEvent"]

EVENT_ACTORS = ("engine", "user", "broker", "scheduler")


class HatEvent(Base):
    __tablename__ = "hat_event"
    __table_args__ = (
        CheckConstraint(
            "actor IN ('engine', 'user', 'broker', 'scheduler')", name="ck_hat_event_actor"
        ),
        Index("hat_event_correlation_id", "correlation_id"),
        Index("hat_event_aggregate", "aggregate_type", "aggregate_id"),
        {"postgresql_partition_by": "RANGE (recorded_at)"},
    )

    # bigint identity on Postgres; a plain integer on SQLite, where `record()` assigns it.
    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    recorded_at = Column(
        Timestamptz, primary_key=True, nullable=False, default=now_utc, server_default=func.now()
    )
    occurred_at = Column(Timestamptz, nullable=False)
    user_id = Column(Uuid, nullable=False)
    bot_id = Column(Uuid)
    aggregate_type = Column(Text, nullable=False)
    aggregate_id = Column(Uuid, nullable=False)
    event_type = Column(Text, nullable=False)
    actor = Column(Text, nullable=False)
    # The signal id for anything trade-related.
    correlation_id = Column(Uuid)
    # The event that caused this one.
    causation_id = Column(BigInteger)
    payload = Column(JsonB, nullable=False)
