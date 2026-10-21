"""Publishing the nightly scan: `hat_watchlist_entry` rows and the `watchlist.published` event
(04 §1, HAT-DEC-T12, 07 §4).

`scan_and_publish` runs the pure `scan.scan_bot` for one bot and writes its rows and one event in
the caller's transaction. It is idempotent: a bot's watchlist for a trade date is written once, so
a job that crashed after this returned and ran again adds nothing. A bot with no live strategy
version or no universe publishes nothing (fail closed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatBot, HatEvent, HatStrategyVersion, HatUniverseVersion, HatWatchlistEntry
from . import lifecycle
from .portfolio.service import members_of
from .scan import ScanInputs, ScanResult, scan_bot
from .strategy.config import parse_config

__all__ = ["EVENT_PUBLISHED", "Published", "bot_universe_members", "scan_and_publish"]

EVENT_PUBLISHED = "watchlist.published"


@dataclass(frozen=True)
class Published:
    """What one publish did: `skipped` names why nothing was scanned or written."""

    rows: int
    gate_on: bool
    skipped: str | None = None


def bot_universe_members(session: Session, bot: HatBot, day: date) -> frozenset[uuid.UUID]:
    """The instruments in the bot's universe on `day`, point in time; empty without one."""
    if bot.universe_version_id is None:
        return frozenset()
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return frozenset() if version is None else members_of(session, version.universe_id, day)


def _already_published(session: Session, bot_id: uuid.UUID, trade_date: date) -> bool:
    for (payload,) in session.execute(
        select(HatEvent.payload).where(
            HatEvent.bot_id == bot_id, HatEvent.event_type == EVENT_PUBLISHED
        )
    ):
        if payload.get("trade_date") == trade_date.isoformat():
            return True
    return False


def scan_and_publish(
    session: Session,
    bot_id: uuid.UUID,
    inputs: ScanInputs,
    trade_date: date,
    now: datetime,
) -> Published:
    """Scan `bot_id` as of `inputs.as_of` and publish the watchlist for `trade_date`.

    `inputs.members` is replaced by the bot's universe on `inputs.as_of` (the caller does not
    choose the universe). Idempotent per (bot, trade date)."""
    bot = session.get(HatBot, bot_id)
    if bot is None or bot.live_version_id is None:
        return Published(0, False, "no_live_version")
    if _already_published(session, bot_id, trade_date):
        return Published(0, False, "already_published")
    version = session.get(HatStrategyVersion, bot.live_version_id)
    if version is None:
        return Published(0, False, "no_live_version")
    config = parse_config(version.config)
    members = bot_universe_members(session, bot, inputs.as_of)
    result: ScanResult = scan_bot(config, replace(inputs, members={str(m) for m in members}))
    for row in result.rows:
        session.add(
            HatWatchlistEntry(
                id=uuid.uuid4(),
                bot_id=bot_id,
                strategy_version_id=version.id,
                playbook=row.playbook,
                instrument_id=uuid.UUID(row.instrument_id),
                trade_date=trade_date,
                score=Decimal(repr(row.score)),
                criteria_results=row.criteria_results,
                pivot=row.pivot,
                planned_stop=row.planned_stop,
                adtr=row.adtr,
                status=lifecycle.WATCHLISTED,
            )
        )
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type=EVENT_PUBLISHED,
        actor="engine",
        payload={
            "trade_date": trade_date.isoformat(),
            "as_of": inputs.as_of.isoformat(),
            "rows": len(result.rows),
            "gate_on": result.gate_on,
            "strategy_version_id": str(version.id),
        },
        bot_id=bot_id,
        occurred_at=now,
    )
    session.flush()
    return Published(len(result.rows), result.gate_on)
