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

from app.hat.events import record
from app.hat.models import HatRegimeModelVersion, HatRegimeState
from app.hat.money import require_exact

from . import regime
from .common import close_of

EVENT_REGIME = "regime.state_recorded"


def ensure_regime_model(session: Session, market_id: uuid.UUID) -> HatRegimeModelVersion:
    """The rules model, version 1, of the market; created on first use (HAT-PORT-034)."""
    row = session.scalars(
        select(HatRegimeModelVersion).where(
            HatRegimeModelVersion.market_id == market_id,
            HatRegimeModelVersion.kind == "rules",
            HatRegimeModelVersion.version == 1,
        )
    ).first()
    if row is None:
        row = HatRegimeModelVersion(
            market_id=market_id,
            kind="rules",
            version=1,
            status="live",
            config=regime.rules_v1_config(),
        )
        session.add(row)
        session.flush()
    return row


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
    model: HatRegimeModelVersion,
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


def regime_table(model: HatRegimeModelVersion) -> dict[str, Decimal]:
    """The momentum-sleeve multiplier table of the model version (stored with it); the defaults
    when the version holds none."""
    stored = (model.config or {}).get("multipliers", {}).get("momentum")
    if not stored:
        return dict(regime.DEFAULT_MULTIPLIERS)
    return {
        state: require_exact(stored[state], f"multipliers.momentum.{state}", floats=True)
        for state in regime.STATES
    }
