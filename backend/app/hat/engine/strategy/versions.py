"""Strategy versions, the criterion catalogue and the two seeds as database operations
(HAT-CRIT-021, HAT-BOT-060/061/070/072/080/084).

Every save creates a new `hat_strategy_version` (a draft) numbered 1, 2, 3 without gaps, with
`parent_version_id` set; `publish_version` freezes a draft by moving it to `backtested`, after
which the config never changes (the database trigger refuses it, and so does this module).
A save is refused inside a market session and when the config does not validate: no row is
written then. Nothing here commits; the caller's transaction decides.

The release gate (HAT-BOT-084, HAT-GOV-083, PR 35): `start_shadow` moves a backtested version to
`shadow`, `promote_version` makes it `live` after 20 trading sessions of shadow and moves the
sleeves of the version it replaces onto it, and `assign_version` points a sleeve at the version it
runs (a sleeve that can place real orders only at a live one). The owner runs them through the
local admin command line (`app.hat.admin`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime
from importlib import resources
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import HatError
from ...events import record
from ...models import (
    HatBacktestRun,
    HatBot,
    HatCriterionType,
    HatMarket,
    HatStrategy,
    HatStrategyVersion,
)
from ...risk.sessions import SessionCalendar, SessionState
from ...time import as_utc, trade_date_of
from ..criteria import REGISTRY
from ..portfolio.lifecycle import LIVE, PROBATION, shadow_gate
from .config import StrategyConfig, parse_config

SEED_NAMES = ("stage2-contraction-breakout", "momentum-breakout-ep")
PERSONAS = {
    "stage2-contraction-breakout": "Minervini-style",
    "momentum-breakout-ep": "Qullamaggie-style",
}
DEFAULT_RISK_CLUSTER = "momentum-long"  # S1 and S2 share one cluster (HAT-PORT-011)
EVENT_CREATED = "strategy.version_created"
EVENT_PROMOTED = "strategy.promoted"
# A sleeve in these lifecycle states places real orders, or may again without a new gate (an
# owner promotion out of Probation), so it only ever runs a live version.
REAL_MONEY_STATES = frozenset({LIVE, PROBATION})


class VersionError(HatError):
    """A version operation was refused (market hours, frozen version, unknown row)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sync_criterion_types(session: Session) -> int:
    """Make `hat_criterion_type` hold exactly the registry: insert missing rows and bring each
    row's stage, `param_schema` and `data_needs` to the registry's. Returns the rows written."""
    existing = {
        (row.code, row.version): row for row in session.scalars(select(HatCriterionType)).all()
    }
    written = 0
    for ctype in REGISTRY.values():
        schema, needs = ctype.param_schema(), list(ctype.data_needs)
        row = existing.pop((ctype.code, ctype.version), None)
        if row is None:
            session.add(
                HatCriterionType(
                    code=ctype.code,
                    version=ctype.version,
                    stage=ctype.stage,
                    param_schema=schema,
                    data_needs=needs,
                )
            )
            written += 1
        elif (row.stage, row.param_schema, row.data_needs) != (ctype.stage, schema, needs):
            row.stage, row.param_schema, row.data_needs = ctype.stage, schema, needs
            written += 1
    session.flush()
    return written


def _versions(session: Session, strategy_id: uuid.UUID) -> list[HatStrategyVersion]:
    return list(
        session.scalars(
            select(HatStrategyVersion)
            .where(HatStrategyVersion.strategy_id == strategy_id)
            .order_by(HatStrategyVersion.version)
        ).all()
    )


def _new_version(
    session: Session,
    strategy: HatStrategy,
    config: StrategyConfig,
    parent_version_id: uuid.UUID | None,
    at: datetime | None,
) -> HatStrategyVersion:
    existing = _versions(session, strategy.id)
    parent = parent_version_id
    if parent is None and existing:
        parent = existing[-1].id
    if parent is not None and parent not in {v.id for v in existing}:
        raise VersionError("unknown_parent")
    row = HatStrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        version=(existing[-1].version if existing else 0) + 1,
        status="draft",
        config=config.to_json(),
        config_hash=config.hash(),
        parent_version_id=parent,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="strategy_version",
        aggregate_id=row.id,
        event_type=EVENT_CREATED,
        actor="engine",
        payload={"strategy": strategy.name, "version": row.version, "config_hash": row.config_hash},
        user_id=strategy.user_id,
        occurred_at=at,
    )
    return row


def save_draft(
    session: Session,
    *,
    strategy_id: uuid.UUID,
    config: Mapping[str, Any] | StrategyConfig,
    calendar: SessionCalendar,
    now: datetime,
    parent_version_id: uuid.UUID | None = None,
) -> HatStrategyVersion:
    """Validate `config` and store it as the strategy's next version (a draft), edited from
    `parent_version_id` (default: the latest version). Raises `ConfigError` for an invalid
    config and `VersionError("market_hours")` unless the calendar says the market is closed at
    `now`; either way nothing is written."""
    # Validate afresh even a built model: it may have been changed since it was parsed.
    parsed = parse_config(config.to_json() if isinstance(config, StrategyConfig) else config)
    if calendar.state(now) is not SessionState.CLOSED:
        raise VersionError("market_hours")
    # The row lock makes concurrent saves take turns, so each reads the true latest version.
    strategy = session.get(HatStrategy, strategy_id, with_for_update=True)
    if strategy is None:
        raise VersionError("unknown_strategy")
    return _new_version(session, strategy, parsed, parent_version_id, now)


def publish_version(
    session: Session, version_id: uuid.UUID, *, backtest_run_id: uuid.UUID
) -> HatStrategyVersion:
    """Freeze a draft: it becomes `backtested` against a completed run of that version, which
    evidences it (HAT-BT-061), and its config can no longer change. Any other starting status, or
    a run of another version or one not completed, is refused."""
    row = session.get(HatStrategyVersion, version_id)
    if row is None:
        raise VersionError("unknown_version")
    if row.status != "draft":
        raise VersionError("not_a_draft")
    run = session.get(HatBacktestRun, backtest_run_id)
    if run is None:
        raise VersionError("unknown_run")
    if run.strategy_version_id != version_id:
        raise VersionError("run_not_for_version")
    if run.status != "completed":
        raise VersionError("run_not_completed")
    row.status = "backtested"
    row.backtest_run_id = backtest_run_id
    session.flush()
    return row


# --- the release gate (HAT-BOT-084) -------------------------------------------------------------


def _closed(calendar: SessionCalendar, now: datetime) -> None:
    if calendar.state(now) is not SessionState.CLOSED:
        raise VersionError("market_hours")


def start_shadow(
    session: Session, version_id: uuid.UUID, *, calendar: SessionCalendar, now: datetime
) -> HatStrategyVersion:
    """A backtested version starts its shadow period now (`shadow_started_at`), outside market
    hours only. Any other status is refused and nothing changes."""
    _closed(calendar, now)
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    if row is None:
        raise VersionError("unknown_version")
    if row.status != "backtested" or row.backtest_run_id is None:
        raise VersionError("not_backtested")
    row.status = "shadow"
    row.shadow_started_at = now
    session.flush()
    return row


def promote_version(
    session: Session,
    version_id: uuid.UUID,
    *,
    calendar: SessionCalendar,
    now: datetime,
    chat_id: int,
) -> HatStrategyVersion:
    """Make a shadow version live (HAT-BOT-084): outside market hours, with its backtest run and
    at least 20 trading sessions of shadow on the calendar. The strategy's previous live
    version is retired first (one live version, HAT-PORT-020's rule for strategies), the sleeves
    that ran it move to this one (their open trades keep their entry version, HAT-BOT-083), and
    `strategy.promoted` is recorded as the owner's (`actor = user`, the allowlisted chat, `via =
    cli`). A refusal changes nothing."""
    _closed(calendar, now)
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    if row is None:
        raise VersionError("unknown_version")
    if row.status != "shadow" or row.backtest_run_id is None or row.shadow_started_at is None:
        raise VersionError("not_in_shadow")
    started: date = trade_date_of(as_utc(row.shadow_started_at))
    if not shadow_gate(calendar.trading_days(), started, trade_date_of(now)):
        raise VersionError("shadow_days")
    previous = session.scalars(
        select(HatStrategyVersion).where(
            HatStrategyVersion.strategy_id == row.strategy_id,
            HatStrategyVersion.status == "live",
        )
    ).first()
    moved: list[HatBot] = []
    if previous is not None:
        previous.status = "retired"
        session.flush()  # the one-live index never sees two
        moved = list(
            session.scalars(select(HatBot).where(HatBot.live_version_id == previous.id)).all()
        )
        for bot in moved:
            bot.live_version_id = row.id
    row.status = "live"
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


def assign_version(session: Session, *, bot_id: uuid.UUID, version_id: uuid.UUID) -> HatBot:
    """Point a sleeve at the version it scans and trades with (`live_version_id`): a version of
    its own strategy that has left draft and is not retired. A sleeve that places real orders
    (Live, Probation) takes only a live version, so an unpromoted version never trades real
    money. Open trades keep their entry version (HAT-BOT-083)."""
    bot = session.get(HatBot, bot_id, with_for_update=True)
    if bot is None:
        raise VersionError("unknown_bot")
    # locked, so a promotion that retires it waits and then moves this sleeve too
    row = session.get(HatStrategyVersion, version_id, with_for_update=True)
    if row is None or row.strategy_id != bot.strategy_id:
        raise VersionError("unknown_version")
    if row.status in ("draft", "retired"):
        raise VersionError("version_not_released")
    if bot.lifecycle_state in REAL_MONEY_STATES and row.status != "live":
        raise VersionError("version_not_live")
    bot.live_version_id = row.id
    session.flush()
    return bot


# --- the two seeds ----------------------------------------------------------------------------


def seed_text(name: str) -> str:
    """The shipped YAML of a seed (verbatim from 04 §6 and §7)."""
    if name not in SEED_NAMES:
        raise VersionError("unknown_seed")
    return resources.files(__package__).joinpath("seeds", f"{name}.yaml").read_text("utf-8")


def seed_config(name: str) -> StrategyConfig:
    return parse_config(yaml.safe_load(seed_text(name)))


def seed_default_strategies(session: Session, user_id: uuid.UUID) -> list[HatStrategy]:
    """Create the two MVP strategies, each with version 1 as a draft, when missing; sync the
    criterion catalogue. Safe to repeat: an existing strategy is left as it is. Needs the NSE
    `hat_market` row."""
    market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
    if market is None:
        raise VersionError("no_market")
    sync_criterion_types(session)
    strategies = []
    for name in SEED_NAMES:
        strategy = session.scalar(
            select(HatStrategy).where(HatStrategy.user_id == user_id, HatStrategy.name == name)
        )
        if strategy is None:
            strategy = HatStrategy(
                id=uuid.uuid4(),
                user_id=user_id,
                market_id=market.id,
                name=name,
                persona=PERSONAS[name],
            )
            session.add(strategy)
            session.flush()
            _new_version(session, strategy, seed_config(name), None, None)
        strategies.append(strategy)
    return strategies


def create_bot(
    session: Session,
    *,
    user_id: uuid.UUID,
    strategy_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    execution_mode: str | None = None,
    risk_cluster: str = DEFAULT_RISK_CLUSTER,
) -> HatBot:
    """A new sleeve for a strategy. It always starts in `shadow` (HAT-BOT-061); a mode change
    goes through the mode-change service, never through creation."""
    if execution_mode not in (None, "shadow"):
        raise VersionError("new_bot_starts_in_shadow")
    bot = HatBot(
        id=uuid.uuid4(),
        user_id=user_id,
        strategy_id=strategy_id,
        broker_account_id=broker_account_id,
        execution_mode="shadow",
        status="active",
        lifecycle_state="research",
        risk_cluster=risk_cluster,
    )
    session.add(bot)
    session.flush()
    return bot


def seed_default_bots(
    session: Session, *, user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> list[HatBot]:
    """One shadow bot per seeded strategy on the given broker account, when missing."""
    bots = []
    for strategy in seed_default_strategies(session, user_id):
        bot = session.scalar(
            select(HatBot).where(
                HatBot.strategy_id == strategy.id, HatBot.broker_account_id == broker_account_id
            )
        )
        bots.append(
            bot
            or create_bot(
                session,
                user_id=user_id,
                strategy_id=strategy.id,
                broker_account_id=broker_account_id,
            )
        )
    return bots


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
                    for v in _versions(session, s.id)
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
