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

The bot's own book is read here too: the instruments it holds and the pivots its positions were
opened on, which the scan keeps off the watchlist (decisions.md "A pivot is used once").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configs.records import Bot, StrategyVersion
from ..configs.unit import configs_of
from ..data.broker_symbols import FYERS_SERIES
from ..data.isin import ordinary_equity
from ..events import record, recorded_for
from ..models import (
    HatEvent,
    HatInstrument,
    HatPosition,
    HatSignal,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..money import require_exact
from . import lifecycle
from .botlock import lock_bot
from .portfolio.universe_service import members_of
from .scan import EarlierRow, ScanInputs, ScanResult, scan_bot
from .strategy.config import StrategyConfig, parse_config

__all__ = ["EVENT_PUBLISHED", "Published", "bot_universe_members", "scan_and_publish"]

EVENT_PUBLISHED = "watchlist.published"


@dataclass(frozen=True)
class Published:
    """What one publish did: `skipped` names why nothing was scanned or written."""

    rows: int
    gate_on: bool
    skipped: str | None = None
    funnel: Mapping[str, Mapping[str, int]] = field(default_factory=dict)  # `ScanResult.funnel`


def _universe_id(session: Session, bot: Bot) -> uuid.UUID | None:
    if bot.universe_version_id is None:
        return None
    version = session.get(HatUniverseVersion, bot.universe_version_id)
    return None if version is None else version.universe_id


def _tradable_members(session: Session, universe_id: uuid.UUID, day: date) -> frozenset[uuid.UUID]:
    """The members of `universe_id` on `day` whose instrument is active, trades under a series
    with a Fyers symbol now (D210: BZ has none, so it never reaches a scan or an entry) and is a
    stock: HAT trades ordinary equity shares only, never an ETF, fund, InvIT or REIT unit or a
    rights entitlement (decisions.md "Stocks only", `isin.ordinary_equity`); their prices are
    kept. Every entry path, the nightly scan, the gap scan and the backtest read it here."""
    members = members_of(session, universe_id, day)
    if not members:
        return members
    rows = session.execute(
        select(HatInstrument.id, HatInstrument.isin).where(
            HatInstrument.id.in_(members),
            HatInstrument.active.is_(True),
            HatInstrument.series.in_(FYERS_SERIES),
        )
    )
    return frozenset(row.id for row in rows if ordinary_equity(row.isin))


def bot_universe_members(session: Session, bot: Bot, day: date) -> frozenset[uuid.UUID]:
    """The tradable instruments in the bot's universe on `day`, point in time; empty without
    one."""
    universe_id = _universe_id(session, bot)
    return frozenset() if universe_id is None else _tradable_members(session, universe_id, day)


def _pivot_on(criteria_results: object) -> date | None:
    value = criteria_results.get("pivot_date") if isinstance(criteria_results, dict) else None
    return date.fromisoformat(value) if isinstance(value, str) else None


def _book(
    session: Session, bot_id: uuid.UUID
) -> tuple[frozenset[str], frozenset[tuple[str, date]]]:
    """(held, used pivots) of `bot_id`, as `ScanInputs` reads them: the instruments of its open
    positions (any state, virtual or real), and the (instrument, pivot date) of the watchlist row
    each of its positions was opened from (its signal's bot, playbook, instrument and trade date;
    a row without a `pivot_date` names no pivot)."""
    held = frozenset(
        str(i)
        for i in session.scalars(
            select(HatPosition.instrument_id).where(
                HatPosition.bot_id == bot_id, HatPosition.closed_at.is_(None)
            )
        )
    )
    entered = session.execute(
        select(HatSignal.instrument_id, HatWatchlistEntry.criteria_results)
        .join(HatPosition, HatPosition.signal_id == HatSignal.id)
        .join(
            HatWatchlistEntry,
            (HatWatchlistEntry.bot_id == HatSignal.bot_id)
            & (HatWatchlistEntry.playbook == HatSignal.playbook)
            & (HatWatchlistEntry.instrument_id == HatSignal.instrument_id)
            & (HatWatchlistEntry.trade_date == HatSignal.trade_date),
        )
        .where(HatPosition.bot_id == bot_id)
    )
    used = frozenset(
        (str(instrument_id), day)
        for instrument_id, results in entered
        if (day := _pivot_on(results)) is not None
    )
    return held, used


def _earlier(
    session: Session, bot_id: uuid.UUID, config: StrategyConfig, day: date
) -> tuple[EarlierRow, ...]:
    """The rows the bot published for the session `day` of its playbooks with a validity window
    (`Playbook.valid_sessions`), which the scan may carry on."""
    windowed = [p.name for p in config.playbooks if p.valid_sessions is not None]
    if not windowed:
        return ()
    rows = session.scalars(
        select(HatWatchlistEntry).where(
            HatWatchlistEntry.bot_id == bot_id,
            HatWatchlistEntry.trade_date == day,
            HatWatchlistEntry.playbook.in_(windowed),
        )
    )
    return tuple(
        EarlierRow(r.playbook, str(r.instrument_id), float(r.score), r.criteria_results or {})
        for r in rows
    )


def _with_members(
    inputs: ScanInputs,
    members: frozenset[uuid.UUID],
    book: tuple[frozenset[str], frozenset[tuple[str, date]]],
    earlier: tuple[EarlierRow, ...],
) -> ScanInputs:
    return replace(
        inputs,
        members={str(m) for m in members},
        held=book[0],
        used_pivots=book[1],
        earlier=earlier,
    )


def _already_published(session: Session, bot_id: uuid.UUID, trade_date: date) -> bool:
    for (payload,) in session.execute(
        select(HatEvent.payload).where(
            HatEvent.bot_id == bot_id,
            HatEvent.event_type == EVENT_PUBLISHED,
            recorded_for(trade_date),
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

    `inputs.members` is replaced by the bot's universe on `inputs.as_of`, `inputs.held` and
    `inputs.used_pivots` by the bot's book (`_book`) and `inputs.earlier` by the rows it
    published for `inputs.as_of` (`_earlier`): the caller chooses none. Idempotent per (bot,
    trade date)."""
    lock_bot(session, bot_id)  # held to commit: a concurrent run waits, then sees our event
    bot = configs_of(session).get(Bot, bot_id)
    if bot is None or bot.live_version_id is None:
        return Published(0, False, "no_live_version")
    if _already_published(session, bot_id, trade_date):
        return Published(0, False, "already_published")
    version = configs_of(session).get(StrategyVersion, bot.live_version_id)
    if version is None:
        return Published(0, False, "no_live_version")
    universe_id = _universe_id(session, bot)
    if universe_id is None:
        return Published(0, False, "no_universe")
    config = parse_config(version.config)
    members = _tradable_members(session, universe_id, inputs.as_of)
    earlier = _earlier(session, bot_id, config, inputs.as_of)
    result: ScanResult = scan_bot(
        config, _with_members(inputs, members, _book(session, bot_id), earlier)
    )
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
                setup_type=row.setup_type,
                valid_until=row.valid_until,
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
    return Published(len(result.rows), result.gate_on, funnel=result.funnel)
