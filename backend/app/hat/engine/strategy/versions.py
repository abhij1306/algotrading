"""Strategy versions and the seeds of the registered strategies as config operations
(HAT-BOT-060/070/072/080).

Every save creates a new strategy version (a draft config record, `configs.records`) numbered
1, 2, 3 without gaps, with `parent_version_id` set; `publish_version` freezes a draft by moving it
to `frozen` against its promotion run folder, after which the config never changes (a version's
config is fixed by its first revision file, `records.record_rules`). A save is refused inside a
market session and when the config does not validate: nothing is written then. Nothing here
commits; the caller's transaction writes the files at its commit. What a version does once
`frozen` (its trial, its release, the sleeves that run it) is the release gate in `release`; the
version states (`governance.VersionStatus`) are named apart from a sleeve's lifecycle states.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...backtest.evidence import COMPLETED, audit_admits, read_evidence
from ...backtest.run_files import read_doc, read_run
from ...configs.records import Strategy, StrategyVersion
from ...configs.unit import configs_of, data_root_of
from ...errors import HatError
from ...events import record
from ...governance import VersionStatus
from ...models import HatMarket
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


def versions_of(session: Session, strategy_id: uuid.UUID) -> list[StrategyVersion]:
    found = configs_of(session).all(StrategyVersion, lambda v: v.strategy_id == strategy_id)
    return sorted(found, key=lambda v: v.version)


def _new_version(
    session: Session,
    strategy: Strategy,
    config: StrategyConfig,
    parent_version_id: uuid.UUID | None,
    at: datetime | None,
) -> StrategyVersion:
    existing = versions_of(session, strategy.id)
    parent = parent_version_id
    if parent is None and existing:
        parent = existing[-1].id
    if parent is not None and parent not in {v.id for v in existing}:
        raise VersionError("unknown_parent")
    row = StrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        version=(existing[-1].version if existing else 0) + 1,
        status=VersionStatus.DRAFT,
        config=config.to_json(),
        config_hash=config.hash(),
        parent_version_id=parent,
    )
    configs_of(session).add(row)
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
) -> StrategyVersion:
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
    # Two concurrent saves read the same latest version; the store refuses the second's commit
    # (`config_unique`: `records.kind_rules`, under the kind's lock), so versions stay unique.
    strategy = configs_of(session).get(Strategy, strategy_id)
    if strategy is None:
        raise VersionError("unknown_strategy")
    if parsed.bot != strategy.name:
        raise VersionError("bot_name_mismatch")
    return _new_version(session, strategy, parsed, parent_version_id, now)


def _not_evidence(root: Path | None, run_id: str) -> str:
    """Why the run `run_id` is no evidence: unknown, not completed, its audit not passed, or its
    `evidence.json` missing or unreadable."""
    run = None if root is None else read_run(root, run_id)
    if root is None or run is None:
        return "unknown_run"
    if (run.get("identity") or {}).get("status") != COMPLETED:
        return "run_not_completed"
    if not audit_admits(read_doc(root, run_id, "audit")):
        return "run_audit_not_passed"
    return "run_without_evidence"


def publish_version(session: Session, version_id: uuid.UUID, *, run_id: str) -> StrategyVersion:
    """Freeze a draft: it becomes `frozen` against a completed run folder of that version
    flagged for promotion (PR 62), which evidences it (HAT-BT-061): the version's
    `backtest_run_id` is the run id. Its config can no longer change. Any other starting status,
    an unknown run, one not completed (no evidence in its folder), one whose run audit failed or
    is missing (`run_audit_not_passed`), one without readable evidence (`run_without_evidence`),
    a run of another version or one not flagged for promotion is refused."""
    row = configs_of(session).get(StrategyVersion, version_id)
    if row is None:
        raise VersionError("unknown_version")
    if row.status != VersionStatus.DRAFT:
        raise VersionError("not_a_draft")
    root = data_root_of(session)
    run = None if root is None else read_evidence(root, run_id)
    if run is None:
        raise VersionError(_not_evidence(root, run_id))
    if run.strategy_version_id != version_id:
        raise VersionError("run_not_for_version")
    if not run.metrics.for_promotion:
        # O5 (PR 62): only a run flagged for promotion evidences; `RunSpec` records one only
        # under the conventions live trades by (never at perfect fills, slots in trigger order)
        raise VersionError("run_not_for_promotion")
    row.status = VersionStatus.FROZEN
    row.backtest_run_id = run_id
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


def seed_default_strategies(session: Session, user_id: uuid.UUID) -> list[Strategy]:
    """Create every registered strategy, each with version 1 as a draft, when missing; sync the
    criterion catalogue. Safe to repeat: an existing strategy is left as it is. Needs the NSE
    `hat_market` row."""
    market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
    if market is None:
        raise VersionError("no_market")
    sync_criterion_types(session)
    strategies = []
    unit = configs_of(session)
    for name, registered in STRATEGIES.items():
        found = unit.all(Strategy, lambda s, name=name: s.user_id == user_id and s.name == name)
        strategy = found[0] if found else None
        if strategy is None:
            strategy = Strategy(
                id=uuid.uuid4(),
                user_id=user_id,
                market_id=market.id,
                name=name,
                persona=registered.persona,
            )
            unit.add(strategy)
            _new_version(session, strategy, seed_config(name), None, None)
        strategies.append(strategy)
    return strategies
