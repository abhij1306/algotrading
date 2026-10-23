"""Reference tables: the instrument row anchors everything else."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
)

from ...base import Base
from ._types import Currency, JsonB, Money, Ratio, Timestamptz, fk, now_utc, uuid_pk

INSTRUMENT_ID = "hat_instrument.id"

__all__ = [
    "HatAnnouncement",
    "HatBrokerSymbol",
    "HatCorporateAction",
    "HatIndustry",
    "HatInstrument",
    "HatInstrumentSymbolHistory",
    "HatMarket",
    "HatPriceBand",
    "HatTradingCalendar",
]


class HatMarket(Base):
    __tablename__ = "hat_market"

    id = uuid_pk()
    code = Column(Text, nullable=False, unique=True)
    timezone = Column(Text, nullable=False)
    currency = Column(Currency, nullable=False)
    session_open = Column(Time, nullable=False)
    session_close = Column(Time, nullable=False)


class HatInstrument(Base):
    __tablename__ = "hat_instrument"
    __table_args__ = (UniqueConstraint("market_id", "isin", "series"),)

    id = uuid_pk()
    market_id = fk("hat_market.id")
    isin = Column(Text, nullable=False)
    series = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    face_value = Column(Money, nullable=False)
    listed_on = Column(Date)
    delisted_on = Column(Date)
    active = Column(Boolean, nullable=False)


class HatInstrumentSymbolHistory(Base):
    __tablename__ = "hat_instrument_symbol_history"

    instrument_id = fk(INSTRUMENT_ID, primary_key=True)
    symbol = Column(Text, nullable=False)
    valid_from = Column(Date, primary_key=True)
    valid_to = Column(Date)


class HatBrokerSymbol(Base):
    __tablename__ = "hat_broker_symbol"
    __table_args__ = (UniqueConstraint("broker", "broker_symbol"),)

    instrument_id = fk(INSTRUMENT_ID, primary_key=True)
    broker = Column(Text, primary_key=True)
    broker_symbol = Column(Text, nullable=False)
    broker_token = Column(Text)
    refreshed_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatPriceBand(Base):
    __tablename__ = "hat_price_band"
    __table_args__ = (CheckConstraint("band_pct IN (2, 5, 10, 20)", name="ck_hat_price_band_pct"),)

    instrument_id = fk(INSTRUMENT_ID, primary_key=True)
    band_pct = Column(Ratio)
    effective_from = Column(Date, primary_key=True)


class HatIndustry(Base):
    __tablename__ = "hat_industry"

    instrument_id = fk(INSTRUMENT_ID, primary_key=True)
    sector = Column(Text, nullable=False)
    industry = Column(Text, nullable=False)
    effective_from = Column(Date, primary_key=True)


class HatCorporateAction(Base):
    __tablename__ = "hat_corporate_action"
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date", "action_type"),)

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    ex_date = Column(Date, nullable=False)
    action_type = Column(Text, nullable=False)
    # Unscaled: a split or bonus factor is never rounded to 4 dp.
    adjustment_factor = Column(Ratio, nullable=False)
    raw_text = Column(Text, nullable=False)
    source = Column(Text, nullable=False)


class HatTradingCalendar(Base):
    __tablename__ = "hat_trading_calendar"

    market_id = fk("hat_market.id", primary_key=True)
    # The column is named `date`; the attribute avoids shadowing the datetime name.
    day = Column("date", Date, primary_key=True)
    is_trading = Column(Boolean, nullable=False)
    open_at = Column(Timestamptz)
    close_at = Column(Timestamptz)
    note = Column(Text)


class HatAnnouncement(Base):
    __tablename__ = "hat_announcement"

    id = uuid_pk()
    guid = Column(Text, nullable=False, unique=True)
    feed = Column(Text, nullable=False)
    instrument_id = Column(Uuid, ForeignKey(INSTRUMENT_ID))
    company_raw = Column(Text, nullable=False)
    category = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    published_at = Column(Timestamptz, nullable=False)
    raw = Column(JsonB, nullable=False)


# Announcements of one instrument, newest first. An existing database that predates
# this index gets it from `PG_DDL` (`create_all` does not add indexes to existing tables).
Index(
    "hat_announcement_instrument_published",
    HatAnnouncement.instrument_id,
    HatAnnouncement.published_at.desc(),
)
