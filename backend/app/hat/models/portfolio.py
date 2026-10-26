"""Portfolio tables. hat_live: the universes, their versions and members (derived from index
data). hat_state: the regime state and the daily allocation."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from ...base import Base, StateBase
from ._types import (
    UNIVERSE_VERSION_STATUSES,
    Currency,
    JsonB,
    Money,
    Ratio,
    Timestamptz,
    fk,
    in_values,
    ref,
    uuid_pk,
)

MARKET_ID = "hat_market.id"

__all__ = [
    "HatAllocationDaily",
    "HatRegimeState",
    "HatUniverse",
    "HatUniverseMember",
    "HatUniverseVersion",
]


class HatUniverse(Base):
    __tablename__ = "hat_universe"
    __table_args__ = (
        CheckConstraint(in_values("kind", ("manual", "screener")), name="ck_hat_universe_kind"),
    )

    id = uuid_pk()
    user_id = Column(Uuid, nullable=False)
    market_id = fk(MARKET_ID)
    name = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)


class HatUniverseVersion(Base):
    __tablename__ = "hat_universe_version"
    __table_args__ = (
        UniqueConstraint("universe_id", "version"),
        CheckConstraint(
            in_values("status", UNIVERSE_VERSION_STATUSES), name="ck_hat_universe_version_status"
        ),
        Index(
            "hat_one_live_universe_version",
            "universe_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
            sqlite_where=text("status = 'live'"),
        ),
    )

    id = uuid_pk()
    universe_id = fk("hat_universe.id")
    version = Column(Integer, nullable=False)
    status = Column(Text, nullable=False)
    definition = Column(JsonB, nullable=False)
    config_hash = Column(Text, nullable=False)
    # the run folder that evidences it (`backtests/runs/<id>`)
    backtest_run_id = ref(nullable=True)
    promoted_at = Column(Timestamptz)


class HatUniverseMember(Base):
    __tablename__ = "hat_universe_member"

    universe_id = fk("hat_universe.id", primary_key=True)
    instrument_id = fk("hat_instrument.id", primary_key=True)
    effective_from = Column(Date, primary_key=True)
    effective_to = Column(Date)
    reason = Column(Text, nullable=False)


class HatRegimeState(StateBase):
    __tablename__ = "hat_regime_state"
    __table_args__ = (
        CheckConstraint(
            in_values("state", ("bull_quiet", "bull_volatile", "correction", "bear")),
            name="ck_hat_regime_state_state",
        ),
    )

    trade_date = Column(Date, primary_key=True)
    market_id = ref(primary_key=True)
    model_version_id = ref(primary_key=True)  # a regime model config record
    state = Column(Text, nullable=False)
    probabilities = Column(JsonB)
    inputs = Column(JsonB, nullable=False)
    confirmed = Column(Boolean, nullable=False)


class HatAllocationDaily(StateBase):
    __tablename__ = "hat_allocation_daily"

    trade_date = Column(Date, primary_key=True)
    bot_id = ref(primary_key=True)
    base_weight = Column(Ratio, nullable=False)
    regime_mult = Column(Ratio, nullable=False)
    health_mult = Column(Ratio, nullable=False)
    final_weight = Column(Ratio, nullable=False)
    capital = Column(Money, nullable=False)
    risk_pct = Column(Ratio, nullable=False)
    currency = Column(Currency, nullable=False)
    # the nominal allocation of a Shadow or Probation sleeve's virtual entries (HAT-PORT-050)
    virtual_capital = Column(Money)
    virtual_risk_pct = Column(Ratio)
