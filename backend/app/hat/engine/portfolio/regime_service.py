"""The regime database operations (HAT-PORT-030..034): the market, the rules model version and
the one regime row per (market, model, day).

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together (HAT-SCH-062). None commits. The rules
they apply are the pure ones in `regime`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.configs.records import RegimeModelVersion
from app.hat.configs.unit import configs_of
from app.hat.engine.strategy.registry import STRATEGIES
from app.hat.events import record
from app.hat.models import HatRegimeState
from app.hat.money import require_exact

from . import regime
from .common import close_of

EVENT_REGIME = "regime.state_recorded"


def _rules_model(session: Session, market_id: uuid.UUID) -> RegimeModelVersion | None:
    found = configs_of(session).all(
        RegimeModelVersion,
        lambda m: m.market_id == market_id and m.kind == "rules" and m.version == 1,
    )
    return found[0] if found else None


def ensure_regime_model(session: Session, market_id: uuid.UUID) -> RegimeModelVersion:
    """The rules model, version 1, of the market; created on first use (HAT-PORT-034)."""
    row = _rules_model(session, market_id)
    if row is None:
        row = RegimeModelVersion(
            market_id=market_id,
            kind="rules",
            version=1,
            status="live",
            config=regime.rules_v1_config(),
        )
        configs_of(session).add(row)
    return row


def regime_profile(strategy_name: str) -> str | None:
    """The regime profile a strategy registers (`registry.Strategy.regime`); None when the
    strategy is not registered."""
    registered = STRATEGIES.get(strategy_name)
    return None if registered is None else registered.regime


def stored_regime_table(
    session: Session, market_id: uuid.UUID, profile: str | None
) -> dict[str, Decimal]:
    """The multiplier table the allocation reads now for `profile`: the stored rules model's
    (`regime_table`), or the one `ensure_regime_model` would store when the model is not created
    yet. Writes nothing."""
    row = _rules_model(session, market_id)
    if row is None:
        return regime.profile_table({regime.MOMENTUM: regime.DEFAULT_MULTIPLIERS}, profile)
    return regime_table(row, profile)


def _confirmation_of(row: HatRegimeState) -> regime.Confirmation:
    data = row.inputs
    return regime.Confirmation(
        str(data["confirmed_state"]), str(data["raw_state"]), int(data["run"])
    )


def regime_row(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> HatRegimeState | None:
    return session.get(HatRegimeState, (day, market_id, model_id))


def latest_regime_before(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> HatRegimeState | None:
    return session.scalars(
        select(HatRegimeState)
        .where(
            HatRegimeState.market_id == market_id,
            HatRegimeState.model_version_id == model_id,
            HatRegimeState.trade_date < day,
        )
        .order_by(HatRegimeState.trade_date.desc())
        .limit(1)
    ).first()


def record_regime(
    session: Session,
    day: date,
    inputs: regime.RegimeInputs,
    *,
    market_id: uuid.UUID,
    model: RegimeModelVersion,
) -> HatRegimeState:
    """Store the regime of `day`: the raw state, the inputs and the confirmation (`confirmed` is
    true when the raw state is the confirmed one). One row per (market, model, day): a rerun
    keeps the first (HAT-PORT-034)."""
    existing = regime_row(session, market_id, model.id, day)
    if existing is not None:
        return existing
    before = latest_regime_before(session, market_id, model.id, day)
    raw = regime.classify_inputs(inputs)
    state = regime.confirm_step(None if before is None else _confirmation_of(before), raw)
    row = HatRegimeState(
        trade_date=day,
        market_id=market_id,
        model_version_id=model.id,
        state=raw,
        probabilities=None,
        inputs={**inputs.to_json(), **state.to_json()},
        confirmed=raw == state.confirmed,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="regime",
        aggregate_id=model.id,
        event_type=EVENT_REGIME,
        actor="scheduler",
        payload={
            "trade_date": day.isoformat(),
            "raw_state": raw,
            "confirmed_state": state.confirmed,
        },
        occurred_at=close_of(day),
    )
    return row


def confirmed_state(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> str | None:
    """The confirmed state stored for `day`; None when the regime job has not run for it."""
    row = regime_row(session, market_id, model_id, day)
    return None if row is None else _confirmation_of(row).confirmed


def regime_table(model: RegimeModelVersion, profile: str | None) -> dict[str, Decimal]:
    """The multiplier table of the regime `profile` in the model version (stored with it,
    `regime.profile_table`); the defaults, as the momentum table, when the version holds none."""
    stored = (model.config or {}).get("multipliers") or {
        regime.MOMENTUM: regime.DEFAULT_MULTIPLIERS
    }
    tables = {
        name: {
            state: require_exact(rows[state], f"multipliers.{name}.{state}", floats=True)
            for state in regime.STATES
        }
        for name, rows in stored.items()
    }
    return regime.profile_table(tables, profile)
