"""The release gate of a strategy version (HAT-BOT-084, HAT-GOV-083) as database operations.

`start_trial` moves a frozen version to its `trial`, `release_version` makes it `released` after
20 trading sessions of trial that a sleeve ran it on paper and moves the sleeves of the version it
supersedes onto it, and
`assign_version` points a sleeve at the version it runs (a sleeve that can place real orders only
at a released one). The version states (`governance.VersionStatus`) are named apart from a
sleeve's lifecycle states (`portfolio.lifecycle`), whose own shadow gate the sleeve still passes.
Every change of a sleeve's version is recorded as `sleeve.version_assigned`.
`release_overview` is what the owner reads before a step. The owner runs them through the local
admin command line (`app.hat.admin`). Nothing here commits.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...events import record
from ...governance import VersionStatus
from ...models import HatBot, HatEvent, HatStrategy, HatStrategyVersion
from ...risk.sessions import SessionCalendar, SessionState
from ...time import as_utc, trade_date_of
from ..owner import OwnerCommand
from ..portfolio.lifecycle import LIVE, PROBATION, SHADOW_DAYS, paper_sessions, shadow_gate
from ..watchlist import EVENT_PUBLISHED
from .versions import VersionError, versions_of

EVENT_PROMOTED = "strategy.promoted"
EVENT_ASSIGNED = "sleeve.version_assigned"
# A sleeve in these lifecycle states places real orders, or may again without a new gate (an
# owner promotion out of Probation), so it only ever runs a released version.
REAL_MONEY_STATES = frozenset({LIVE, PROBATION})


def _closed(calendar: SessionCalendar, now: datetime) -> None:
    if calendar.state(now) is not SessionState.CLOSED:
        raise VersionError("market_hours")


def start_trial(
    session: Session, version_id: uuid.UUID, *, calendar: SessionCalendar, now: datetime
) -> HatStrategyVersion:
    """A frozen version starts its trial (paper) period now (`shadow_started_at`), outside market
    hours only. Any other status is refused and nothing changes."""
    _closed(calendar, now)
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    if row is None:
        raise VersionError("unknown_version")
    if row.status != VersionStatus.FROZEN or row.backtest_run_id is None:
        raise VersionError("not_frozen")
    row.status = VersionStatus.TRIAL
    row.shadow_started_at = now
    session.flush()
    return row


def release_version(
    session: Session,
    version_id: uuid.UUID,
    command: OwnerCommand,
) -> HatStrategyVersion:
    """Release a version in trial (HAT-BOT-084): outside market hours, with its backtest run, at
    least 20 trading sessions of trial on the calendar and at least 20 of them that a sleeve of the
    strategy ran it on paper (`trial_sessions`). The strategy's previously released
    version is superseded first (one released version, HAT-PORT-020's rule), the sleeves
    that ran it move to this one (their open trades keep their entry version, HAT-BOT-083), and
    `strategy.promoted` is recorded as the owner's (`actor = user`, the allowlisted chat, `via =
    cli`). A refusal changes nothing."""
    calendar, now, chat_id = command.calendar, command.now, command.chat_id
    _closed(calendar, now)
    strategy_id = session.scalar(
        select(HatStrategyVersion.strategy_id).where(HatStrategyVersion.id == version_id)
    )
    if strategy_id is None:
        raise VersionError("unknown_version")
    # sleeves before versions, the order `assign_version` locks in, so the two never deadlock
    sleeves = list(
        session.scalars(
            select(HatBot)
            .where(HatBot.strategy_id == strategy_id)
            .order_by(HatBot.id)
            .with_for_update()
        ).all()
    )
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    assert row is not None  # versions are never deleted
    if (
        row.status != VersionStatus.TRIAL
        or row.backtest_run_id is None
        or row.shadow_started_at is None
    ):
        raise VersionError("not_in_trial")
    started: date = trade_date_of(as_utc(row.shadow_started_at))
    today = trade_date_of(now)
    if not shadow_gate(calendar.trading_days(), started, today):
        raise VersionError("trial_days")
    if trial_sessions(session, row, sleeves, calendar, today) < SHADOW_DAYS:
        raise VersionError("trial_sessions")
    previous = session.scalars(
        select(HatStrategyVersion)
        .where(
            HatStrategyVersion.strategy_id == row.strategy_id,
            HatStrategyVersion.status == VersionStatus.RELEASED,
        )
        .with_for_update()
    ).first()
    moved: list[HatBot] = []
    if previous is not None:
        previous.status = VersionStatus.SUPERSEDED
        session.flush()  # the one-released index never sees two
        moved = [b for b in sleeves if b.live_version_id == previous.id]
        for bot in moved:
            _assigned(session, bot, row, chat_id=chat_id, now=now)
    row.status = VersionStatus.RELEASED
    row.promoted_at = now
    session.flush()
    strategy = session.get(HatStrategy, row.strategy_id)
    assert strategy is not None  # the foreign key holds it
    record(
        session,
        aggregate_type="strategy_version",
        aggregate_id=row.id,
        event_type=EVENT_PROMOTED,
        actor="user",
        payload={
            "strategy": strategy.name,
            "version": row.version,
            "replaced": None if previous is None else previous.version,
            "bots": sorted(str(b.id) for b in moved),
            "chat_id": chat_id,
            "via": "cli",
        },
        user_id=strategy.user_id,
        occurred_at=now,
    )
    return row


def trial_sessions(
    session: Session,
    row: HatStrategyVersion,
    sleeves: list[HatBot],
    calendar: SessionCalendar,
    today: date,
) -> int:
    """The trading sessions of `row`'s trial that a sleeve ran it on paper: the trade dates of the
    sleeves' `watchlist.published` events naming the version (`lifecycle.paper_sessions`). A
    version in trial runs on no Live or Probation sleeve (`assign_version`), so each is paper."""
    if row.shadow_started_at is None or not sleeves:
        return 0
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.event_type == EVENT_PUBLISHED,
            HatEvent.bot_id.in_([b.id for b in sleeves]),
            HatEvent.occurred_at >= row.shadow_started_at,
        )
    ).all()
    version = str(row.id)
    published = (
        date.fromisoformat(p["trade_date"])
        for p in payloads
        if p.get("strategy_version_id") == version
    )
    started = trade_date_of(as_utc(row.shadow_started_at))
    return paper_sessions(published, calendar.trading_days(), started, today)


def assign_version(
    session: Session,
    *,
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    command: OwnerCommand,
) -> HatBot:
    """Point a sleeve at the version it scans and trades with (`live_version_id`): a version of
    its own strategy that is frozen, in trial or released. A sleeve that places real orders
    (Live, Probation) takes only a released version, so an unreleased version never trades real
    money. Open trades keep their entry version (HAT-BOT-083). The change is recorded as the
    owner's `sleeve.version_assigned` (PR 73); naming the version the sleeve already runs changes
    nothing and records nothing."""
    bot = session.get(HatBot, bot_id, with_for_update=True)
    if bot is None:
        raise VersionError("unknown_bot")
    # locked after the sleeve, the order `release_version` locks in
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    if row is None or row.strategy_id != bot.strategy_id:
        raise VersionError("unknown_version")
    if row.status in (VersionStatus.DRAFT, VersionStatus.SUPERSEDED):
        raise VersionError("version_not_assignable")
    if bot.lifecycle_state in REAL_MONEY_STATES and row.status != VersionStatus.RELEASED:
        raise VersionError("version_not_released")
    if bot.live_version_id == row.id:  # a repeated step: nothing changes, nothing is recorded
        return bot
    _assigned(session, bot, row, chat_id=command.chat_id, now=command.now)
    session.flush()
    return bot


def _assigned(
    session: Session, bot: HatBot, row: HatStrategyVersion, *, chat_id: int, now: datetime
) -> None:
    """Point `bot` at `row` and record `sleeve.version_assigned` as the owner's (`actor = user`,
    the allowlisted chat, `via = cli`), with the version it ran before (None for its first)."""
    before = (
        None
        if bot.live_version_id is None
        else session.get(HatStrategyVersion, bot.live_version_id)
    )
    bot.live_version_id = row.id
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot.id,
        event_type=EVENT_ASSIGNED,
        actor="user",
        payload={
            "version_id": str(row.id),
            "version": row.version,
            "replaced": None if before is None else before.version,
            "chat_id": chat_id,
            "via": "cli",
        },
        bot_id=bot.id,
        user_id=bot.user_id,
        occurred_at=now,
    )


def release_overview(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """The user's strategies with their versions, and the sleeves with what they run: what the
    owner reads before a release step (ids included, since the steps take them)."""
    strategies = session.scalars(
        select(HatStrategy).where(HatStrategy.user_id == user_id).order_by(HatStrategy.name)
    ).all()
    bots = session.scalars(
        select(HatBot).where(HatBot.user_id == user_id).order_by(HatBot.id)
    ).all()
    return {
        "strategies": [
            {
                "id": str(s.id),
                "name": s.name,
                "versions": [
                    {
                        "id": str(v.id),
                        "version": v.version,
                        "status": v.status,
                        "backtest_run_id": None
                        if v.backtest_run_id is None
                        else str(v.backtest_run_id),
                        "shadow_started_at": None
                        if v.shadow_started_at is None
                        else as_utc(v.shadow_started_at).isoformat(),
                    }
                    for v in versions_of(session, s.id)
                ],
            }
            for s in strategies
        ],
        "bots": [
            {
                "id": str(b.id),
                "strategy_id": str(b.strategy_id),
                "lifecycle_state": b.lifecycle_state,
                "execution_mode": b.execution_mode,
                "status": b.status,
                "live_version_id": None if b.live_version_id is None else str(b.live_version_id),
                "universe_version_id": None
                if b.universe_version_id is None
                else str(b.universe_version_id),
                "broker_account_id": str(b.broker_account_id),
            }
            for b in bots
        ],
    }
