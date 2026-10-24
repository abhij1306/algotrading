"""Strategy versions and the seeds of the registered strategies as database operations
(HAT-BOT-060/070/072/080).

Every save creates a new `hat_strategy_version` (a draft) numbered 1, 2, 3 without gaps, with
`parent_version_id` set; `publish_version` freezes a draft by moving it to `backtested`, after
which the config never changes (the database trigger refuses it, and so does this module).
A save is refused inside a market session and when the config does not validate: no row is
written then. Nothing here commits; the caller's transaction decides. What a version does after
`backtested` (shadow, live, the sleeves that run it) is the release gate in `release`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...backtest.evidence import RunMetrics
from ...errors import HatError
from ...events import record
from ...models import HatBacktestRun, HatMarket, HatStrategy, HatStrategyVersion
from ...risk.sessions import SessionCalendar, SessionState
from .catalogue import sync_criterion_types
from .config import StrategyConfig, parse_config
from .registry import STRATEGIES

EVENT_CREATED = "strategy.version_created"


class VersionError(HatError):
    """A version operation was refused (market hours, frozen version, unknown row)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def versions_of(session: Session, strategy_id: uuid.UUID) -> list[HatStrategyVersion]:
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
    existing = versions_of(session, strategy.id)
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
    config, `VersionError("market_hours")` unless the calendar says the market is closed at
    `now`, and `VersionError("bot_name_mismatch")` when the config's `bot` is not the strategy's
    name, the one identity sizing, allocation and evidence key on; either way nothing is
    written."""
    # Validate afresh even a built model: it may have been changed since it was parsed.
    parsed = parse_config(config.to_json() if isinstance(config, StrategyConfig) else config)
    if calendar.state(now) is not SessionState.CLOSED:
        raise VersionError("market_hours")
    # The row lock makes concurrent saves take turns, so each reads the true latest version.
    strategy = session.get(HatStrategy, strategy_id, with_for_update=True)
    if strategy is None:
        raise VersionError("unknown_strategy")
    if parsed.bot != strategy.name:
        raise VersionError("bot_name_mismatch")
    return _new_version(session, strategy, parsed, parent_version_id, now)


def publish_version(
    session: Session, version_id: uuid.UUID, *, backtest_run_id: uuid.UUID
) -> HatStrategyVersion:
    """Freeze a draft: it becomes `backtested` against a completed run of that version, which
    evidences it (HAT-BT-061), and its config can no longer change. Any other starting status, or
    a run of another version, one not completed or one not flagged for promotion (PR 62), is
    refused."""
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
    if not RunMetrics.from_json(run.metrics).for_promotion:
        # O5 (PR 62): only a run flagged for promotion evidences; `RunSpec` records one only
        # under the conventions live trades by (never at perfect fills, slots in trigger order)
        raise VersionError("run_not_for_promotion")
    row.status = "backtested"
    row.backtest_run_id = backtest_run_id
    session.flush()
    return row


# --- the seeds (one per registered strategy) ----------------------------------------------------


def seed_text(name: str) -> str:
    """The shipped config YAML of a seed (verbatim, without its registration header)."""
    strategy = STRATEGIES.get(name)
    if strategy is None:
        raise VersionError("unknown_seed")
    return strategy.config_text


def seed_config(name: str) -> StrategyConfig:
    return parse_config(yaml.safe_load(seed_text(name)))


def seed_default_strategies(session: Session, user_id: uuid.UUID) -> list[HatStrategy]:
    """Create every registered strategy, each with version 1 as a draft, when missing; sync the
    criterion catalogue. Safe to repeat: an existing strategy is left as it is. Needs the NSE
    `hat_market` row."""
    market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
    if market is None:
        raise VersionError("no_market")
    sync_criterion_types(session)
    strategies = []
    for name, registered in STRATEGIES.items():
        strategy = session.scalar(
            select(HatStrategy).where(HatStrategy.user_id == user_id, HatStrategy.name == name)
        )
        if strategy is None:
            strategy = HatStrategy(
                id=uuid.uuid4(),
                user_id=user_id,
                market_id=market.id,
                name=name,
                persona=registered.persona,
            )
            session.add(strategy)
            session.flush()
            _new_version(session, strategy, seed_config(name), None, None)
        strategies.append(strategy)
    return strategies
