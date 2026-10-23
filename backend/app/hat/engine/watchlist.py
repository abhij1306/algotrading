"""Publishing the nightly scan: `hat_watchlist_entry` rows and the `watchlist.published` event
(HAT-DEC-T12).

`scan_and_publish` runs the pure `scan.scan_bot` for one bot and writes its rows and one event in
the caller's transaction. It is idempotent: a bot's watchlist for a trade date is written once, so
a job that crashed after this returned and ran again adds nothing, and two runs at the same time
are serialised by the bot's lock (the second sees the first one's event). A bot with no live
strategy version or no universe publishes nothing and records nothing (fail closed), so a run
after the universe is assigned still publishes that day.

The members are read point in time from the universe the bot's version names (HAT-PORT-021):
membership belongs to the universe, and a version pins its definition, not a frozen list. Only
the active members trading under a series with a Fyers symbol (EQ, BE) are tradable: one moved
to BZ or any other series, or gone from the listing (a stock in BZ today is, while its bhavcopy
rows go on), is never scanned or entered (D210), whatever its strategy's criteria say.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.instruments import FYERS_SERIES
from ..events import record
from ..models import (
    HatBot,
    HatEvent,
    HatInstrument,
    HatStrategyVersion,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..money import require_exact
from . import lifecycle
from .botlock import lock_bot
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


def _universe_id(session: Session, bot: HatBot) -> uuid.UUID | None:
    if bot.universe_version_id is None:
        return None
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return None if version is None else version.universe_id


def _tradable_members(session: Session, universe_id: uuid.UUID, day: date) -> frozenset[uuid.UUID]:
    """The members of `universe_id` on `day` whose instrument is active and trades under a series
    with a Fyers symbol now (D210: BZ has none, so it never reaches a scan or an entry)."""
    members = members_of(session, universe_id, day)
    if not members:
        return members
    return frozenset(
        session.scalars(
            select(HatInstrument.id).where(
                HatInstrument.id.in_(members),
                HatInstrument.active.is_(True),
                HatInstrument.series.in_(FYERS_SERIES),
            )
        )
    )


def bot_universe_members(session: Session, bot: HatBot, day: date) -> frozenset[uuid.UUID]:
    """The tradable instruments in the bot's universe on `day`, point in time; empty without
    one."""
    universe_id = _universe_id(session, bot)
    return frozenset() if universe_id is None else _tradable_members(session, universe_id, day)


def _with_members(inputs: ScanInputs, members: frozenset[uuid.UUID]) -> ScanInputs:
    return ScanInputs(
        as_of=inputs.as_of,
        members={str(m) for m in members},
        bars=inputs.bars,
        regime_closes=inputs.regime_closes,
        features=inputs.features,
        series=inputs.series,
        price_bands=inputs.price_bands,
    )


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
    lock_bot(session, bot_id)  # held to commit: a concurrent run waits, then sees our event
    bot = session.get(HatBot, bot_id)
    if bot is None or bot.live_version_id is None:
        return Published(0, False, "no_live_version")
    if _already_published(session, bot_id, trade_date):
        return Published(0, False, "already_published")
    version = session.get(HatStrategyVersion, bot.live_version_id)
    if version is None:
        return Published(0, False, "no_live_version")
    universe_id = _universe_id(session, bot)
    if universe_id is None:
        return Published(0, False, "no_universe")
    config = parse_config(version.config)
    members = _tradable_members(session, universe_id, inputs.as_of)
    result: ScanResult = scan_bot(config, _with_members(inputs, members))
    for row in result.rows:
        session.add(
            HatWatchlistEntry(
                id=uuid.uuid4(),
                bot_id=bot_id,
                strategy_version_id=version.id,
                playbook=row.playbook,
                instrument_id=uuid.UUID(row.instrument_id),
                trade_date=trade_date,
                score=require_exact(row.score, "score", floats=True),
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
