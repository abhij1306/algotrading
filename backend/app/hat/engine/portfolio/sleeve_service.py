"""The sleeve lifecycle database operations (HAT-PORT-060..065): the evidence the guards read,
the one writer of `hat_bot.lifecycle_state` (`transition`), the owner's moves and the engine's own.

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together (HAT-SCH-062). None commits. The rules
they apply are the pure ones in `lifecycle` and `health`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.backtest.evidence import backtest_evidence
from app.hat.data.calendar import session_calendar
from app.hat.engine.owner import OwnerCommand
from app.hat.events import record
from app.hat.models import (
    HatBot,
    HatDailyBotSnapshot,
    HatEvent,
    HatPosition,
    HatSignal,
    HatStrategy,
    HatStrategyVersion,
    HatTradeResult,
)
from app.hat.risk.policy import RiskPolicy
from app.hat.time import as_utc, trade_date_of, utc_now

from . import lifecycle
from .common import PortfolioError, close_of
from .health import Health, health_of

EVENT_LIFECYCLE = "sleeve.lifecycle_changed"


@dataclass(frozen=True)
class Actor:
    """Who moves a sleeve and when, as its `sleeve.lifecycle_changed` event records it: the
    engine by default; the owner (`actor = user`) with the allowlisted chat and the channel
    (`via`). `at` None is the current instant."""

    name: str = "engine"
    chat_id: int | None = None
    by_owner: bool = False
    via: str | None = None
    at: datetime | None = None

    @classmethod
    def owner(cls, command: OwnerCommand) -> Actor:
        """The owner's move from the admin command line."""
        return cls("user", command.chat_id, by_owner=True, via="cli", at=command.now)


ENGINE = Actor()


def stage_entered_on(session: Session, bot_id: uuid.UUID, stage: str) -> date | None:
    """The trade date the sleeve last entered `stage`, from its lifecycle events."""
    events = session.scalars(
        select(HatEvent)
        .where(HatEvent.aggregate_id == bot_id, HatEvent.event_type == EVENT_LIFECYCLE)
        .order_by(HatEvent.id.desc())
    )
    for event in events:
        if event.payload.get("to") == stage:
            return trade_date_of(as_utc(event.occurred_at))
    return None


def trade_results(
    session: Session,
    bot_id: uuid.UUID,
    *,
    virtual: bool,
    since: datetime | None = None,
    until: datetime | None = None,
    exclude: Collection[tuple[uuid.UUID, str]] = (),
) -> tuple[Decimal, ...]:
    """The R multiples of the sleeve's closed real (or shadow) trades closed from `since` to
    `until` (both inclusive, either open), oldest first. A replay of an earlier trade date passes
    its own close as `until`, so later trades never change an earlier decision. The veto trades
    of skipped confirm signals (virtual, but not shadow trades) are in neither: they feed the
    veto score only (HAT-EXEC-130). A trade whose (strategy version, playbook) is in `exclude` is
    in neither (PR 40)."""
    query = (
        select(
            HatTradeResult.r_multiple,
            HatPosition.closed_at,
            HatSignal.strategy_version_id,
            HatSignal.playbook,
        )
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatPosition.bot_id == bot_id, HatPosition.is_virtual.is_(virtual))
        .where(HatPosition.is_virtual.is_(False) | (HatSignal.mode == "shadow"))
        .order_by(HatPosition.closed_at)
    )
    rows = session.execute(query).all()
    return tuple(
        Decimal(r)
        for r, closed, version_id, playbook in rows
        if (version_id, playbook) not in exclude
        and (since is None or (closed is not None and as_utc(closed) >= as_utc(since)))
        and (until is None or (closed is not None and as_utc(closed) <= as_utc(until)))
    )


def latest_snapshot(
    session: Session, bot_id: uuid.UUID, before: date
) -> HatDailyBotSnapshot | None:
    return session.scalars(
        select(HatDailyBotSnapshot)
        .where(HatDailyBotSnapshot.bot_id == bot_id, HatDailyBotSnapshot.trade_date < before)
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()


def sleeve_health(
    session: Session, bot_id: uuid.UUID, trade_date: date, policy: RiskPolicy
) -> Health:
    """The health multiplier from the sleeve's latest snapshot before `trade_date`; the full
    value when it has none yet (a new sleeve has no drawdown)."""
    snap = latest_snapshot(session, bot_id, trade_date)
    if snap is None:
        return Health(policy.health_full_mult, False)
    return health_of(Decimal(snap.equity), Decimal(snap.equity_peak), policy)


def load_evidence(
    session: Session,
    bot: HatBot,
    trade_date: date,
    policy: RiskPolicy,
) -> lifecycle.Evidence:
    """Everything the guards read for `bot` on `trade_date`."""
    facts = backtest_evidence(session, bot)
    calendar = session_calendar(session)
    entered = stage_entered_on(session, bot.id, lifecycle.PROBATION)
    health_zero = False
    if bot.lifecycle_state == lifecycle.LIVE:
        health_zero = sleeve_health(session, bot.id, trade_date, policy).probation
    return lifecycle.Evidence(
        today=trade_date,
        backtest=facts,
        sessions=frozenset(calendar.trading_days()) if calendar is not None else frozenset(),
        shadow_started_on=stage_entered_on(session, bot.id, lifecycle.SHADOW),
        health_zero=health_zero,
        # a playbook the run cannot have is not compared with it (PR 40)
        live_r=trade_results(
            session,
            bot.id,
            virtual=False,
            until=close_of(trade_date),
            exclude=facts.unbacktested,
        ),
        probation_entered_on=entered,
        probation_shadow_r=trade_results(
            session,
            bot.id,
            virtual=True,
            since=None if entered is None else close_of(entered),
            until=close_of(trade_date),
            exclude=facts.unbacktested,
        ),
    )


def transition(
    session: Session,
    bot_id: uuid.UUID,
    target: str,
    evidence: lifecycle.Evidence,
    actor: Actor = ENGINE,
) -> HatBot:
    """Move a sleeve along one allowed edge and append `sleeve.lifecycle_changed` in the same
    transaction. Raises `LifecycleError` (changing nothing) when the edge or its guard is not
    met. This is the only code that writes `hat_bot.lifecycle_state` (CONF
    `lifecycle_single_writer`). The guard reads the state under the row's lock, re-read, so an
    owner move and the engine's own never both leave the same state."""
    bot = session.get(HatBot, bot_id, with_for_update=True, populate_existing=True)
    if bot is None:
        raise PortfolioError("bot_unknown")
    verdict = lifecycle.guard(bot.lifecycle_state, target, evidence, by_owner=actor.by_owner)
    if not verdict.allowed:
        raise lifecycle.LifecycleError(verdict.reason)
    if actor.by_owner and actor.name != "user":
        raise lifecycle.LifecycleError("owner_actor")
    before = bot.lifecycle_state
    bot.lifecycle_state = target
    payload: dict[str, object] = {"from": before, "to": target, "reason": verdict.reason}
    if actor.name == "user":
        payload["chat_id"] = actor.chat_id
    if actor.via is not None:
        payload["via"] = actor.via
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type=EVENT_LIFECYCLE,
        actor=actor.name,
        payload=payload,
        bot_id=bot_id,
        occurred_at=actor.at or utc_now(),
    )
    session.flush()
    return bot


def owner_transition(
    session: Session,
    bot_id: uuid.UUID,
    target: str,
    policy: RiskPolicy,
    command: OwnerCommand,
) -> HatBot:
    """The owner moves a sleeve along an edge (HAT-PORT-060, PR 35): the same guard on the same
    evidence of the command's trade date as the engine's moves, recorded as the owner's
    (`Actor.owner`). Outside market hours only, so a sleeve never starts routing differently
    inside a session. A sleeve goes Live only on a live strategy version (HAT-BOT-084), so a
    version still in its shadow period never trades real money. A refusal changes nothing."""
    if command.in_session:
        raise lifecycle.LifecycleError("market_hours")
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise PortfolioError("bot_unknown")
    if target == lifecycle.LIVE:
        version = (
            None
            if bot.live_version_id is None
            else session.get(HatStrategyVersion, bot.live_version_id)
        )
        if version is None or version.status != "live":
            raise lifecycle.LifecycleError("version_not_live")
        # one sleeve of a strategy holds real money: a second would take its base weight twice.
        # The strategy row's lock orders two owners promoting sibling sleeves at once: the
        # second reads the first's committed Live sleeve (the engine's own edges never enter
        # Live, so this is the one way in).
        session.get(HatStrategy, bot.strategy_id, with_for_update=True)
        holder = session.scalar(
            select(HatBot.id).where(
                HatBot.strategy_id == bot.strategy_id,
                HatBot.id != bot.id,
                HatBot.lifecycle_state.in_((lifecycle.LIVE, lifecycle.PROBATION)),
            )
        )
        if holder is not None:
            raise lifecycle.LifecycleError("strategy_has_live_sleeve")
    evidence = load_evidence(session, bot, command.trade_date, policy)
    return transition(session, bot_id, target, evidence, Actor.owner(command))


def advance_automatic(
    session: Session,
    trade_date: date,
    policy: RiskPolicy,
) -> list[tuple[uuid.UUID, str]]:
    """The edges the engine takes by itself: Live -> Probation when the trigger fires, and
    Probation -> Retired when its gate opens. Returns (bot, new state) for each move. Safe to
    repeat: a sleeve already moved no longer matches."""
    moved: list[tuple[uuid.UUID, str]] = []
    engine = Actor(at=close_of(trade_date))
    for bot in session.scalars(select(HatBot).order_by(HatBot.id)).all():
        target = {lifecycle.LIVE: lifecycle.PROBATION, lifecycle.PROBATION: lifecycle.RETIRED}.get(
            bot.lifecycle_state
        )
        if target is None:
            continue
        evidence = load_evidence(session, bot, trade_date, policy)
        if lifecycle.guard(bot.lifecycle_state, target, evidence).allowed:
            transition(session, bot.id, target, evidence, engine)
            moved.append((bot.id, target))
    return moved
