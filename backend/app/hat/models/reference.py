"""Reference tables: the instrument row anchors everything else."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    func,
)

from ...base import Base
from ._types import Currency, JsonB, Money, Ratio, Timestamptz, fk, now_utc, uuid_pk

INSTRUMENT_ID = "hat_instrument.id"

__all__ = [
    "HatAnnouncement",
    "HatBrokerSymbol",
    "HatCorporateAction",
    "HatCorporateActionVersion",
    "HatDemergerNotice",
    "HatIndustry",
    "HatInstrument",
    "HatInstrumentSymbolHistory",
    "HatIsinLineage",
    "HatLineageOverride",
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
    """One security, identified by its ISIN (audit D5). `series` is the series it trades under
    now, the one its broker symbol names; `series_since` is the day a listing refresh first saw
    it (None: since listing) and `previous_series` the one before. The series of each past day is
    on that day's `bars_daily_raw` row."""

    __tablename__ = "hat_instrument"
    __table_args__ = (UniqueConstraint("market_id", "isin"),)

    id = uuid_pk()
    market_id = fk("hat_market.id")
    isin = Column(Text, nullable=False)
    series = Column(Text, nullable=False)
    series_since = Column(Date)
    previous_series = Column(Text)
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
    __table_args__ = (
        UniqueConstraint("instrument_id", "ex_date", "action_type"),
        # One factor <= 0 or NaN would stop every adjusted reader (audit L2); `schema.PG_DDL`
        # adds it to a table created before it, with the same text.
        CheckConstraint(
            "adjustment_factor > 0 AND adjustment_factor <> 'NaN'",
            name="hat_corporate_action_factor_check",
        ),
        # CA5: a known date has its basis and is never before the ex-date; both are NULL when
        # nobody knows (`data/ca_known.py`). `schema.PG_DDL` adds it with the same text.
        CheckConstraint(
            "(known_on IS NULL AND known_basis IS NULL) OR (known_on IS NOT NULL AND known_basis"
            " IS NOT NULL AND known_on >= ex_date AND known_basis IN ('ex_open', 'settled_close',"
            " 'resulting_first_close', 'research_source_date', 'correction'))",
            name="hat_corporate_action_known_check",
        ),
    )

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    ex_date = Column(Date, nullable=False)
    action_type = Column(Text, nullable=False)
    # Unscaled: a split or bonus factor is never rounded to 4 dp.
    adjustment_factor = Column(Ratio, nullable=False)
    raw_text = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    # A dividend's cash per share in rupees (`data/ca_dividends.py`); NULL on every other type
    # and on a dividend whose amount is not known. Price-neutral: no view applies it.
    cash_amount = Column(Ratio)
    # CA5, point in time (`data/ca_known.py`): the first NSE filing naming the event (for a
    # demerger, its record date: the backtest's forced exit reads it, CA8
    # `data/demerger_notice.py`), and the first date a backtest could have used the factor with
    # the fact it rests on.
    announced_on = Column(Date)
    known_on = Column(Date)
    known_basis = Column(Text)


class HatDemergerNotice(Base):
    """CA8: a demerger NSE's daily corporate-action file (`bc`) listed before its factor is known
    (`data/demerger_notice.py`): the parent and its ex-date, and the night HAT first read it
    (`announced_on`). A position held into it exits at the close of the last session before
    the ex-date (`engine/demerger_exit.py`); the factor still comes from the ex-date gap."""

    __tablename__ = "hat_demerger_notice"
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date"),)

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    ex_date = Column(Date, nullable=False)
    announced_on = Column(Date, nullable=False)
    source = Column(Text, nullable=False)
    raw_text = Column(Text, nullable=False)
    recorded_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatCorporateActionVersion(Base):
    """CA6: every value a corporate action ever had, appended in the transaction of the write
    that made it and never changed (`data/ca_versions.py`; `schema.PG_DDL` refuses an UPDATE or a
    DELETE). `hat_corporate_action` is the hindsight projection of these rows."""

    __tablename__ = "hat_corporate_action_version"
    __table_args__ = (
        UniqueConstraint("instrument_id", "ex_date", "action_type", "seq"),
        CheckConstraint(
            "status IN ('accepted', 'deleted', 'conflict', 'cross_check', 'disputed')",
            name="ck_hat_corporate_action_version_status",
        ),
        CheckConstraint(
            "factor > 0 AND factor <> 'NaN'", name="ck_hat_corporate_action_version_factor"
        ),
        CheckConstraint(
            "available_on IS NULL OR available_on >= ex_date",
            name="ck_hat_corporate_action_version_available",
        ),
    )

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    ex_date = Column(Date, nullable=False)
    action_type = Column(Text, nullable=False)
    seq = Column(Integer, nullable=False)  # 1, 2, ... per (instrument, ex-date, type): precedence
    status = Column(Text, nullable=False)
    factor = Column(Ratio, nullable=False)
    cash = Column(Ratio)
    basis = Column(Text)
    source = Column(Text, nullable=False)
    text = Column(Text, nullable=False)
    source_published_on = Column(Date)
    first_observed_at = Column(
        Timestamptz, nullable=False, default=now_utc, server_default=func.now()
    )
    available_on = Column(Date)  # NULL: unknown, never in a point-in-time selection
    band_low = Column(Ratio)  # a demerger's estimates, min..max (`ca_demergers`)
    band_high = Column(Ratio)


class HatIsinLineage(Base):
    """One ISIN succession `data/lineage.py` found: the old ISIN's instrument, the new one's,
    the new ISIN's first session and the verdict. `linked` rows make one price history of a
    chain (at most one linked successor and predecessor per instrument); `review` rows are the
    owner's review list and change nothing."""

    __tablename__ = "hat_isin_lineage"
    __table_args__ = (
        UniqueConstraint("old_instrument_id", "new_instrument_id"),
        CheckConstraint("status IN ('linked', 'review')", name="ck_hat_isin_lineage_status"),
        CheckConstraint("old_instrument_id <> new_instrument_id", name="ck_hat_isin_lineage_pair"),
    )

    id = uuid_pk()
    old_instrument_id = fk(INSTRUMENT_ID)
    new_instrument_id = fk(INSTRUMENT_ID)
    effective_date = Column(Date, nullable=False)
    status = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    evidence = Column(JsonB, nullable=False)
    decided_at = Column(Timestamptz, nullable=False, default=now_utc)
    # CA5: the last bar the link rules read (the new ISIN's `EDGE_DAYS`-th session), so the
    # first day a backtest could have known the link; NULL when the bars do not reach it.
    known_on = Column(Date)


# A chain never forks: one linked successor per instrument and one linked predecessor.
_LINKED = HatIsinLineage.status == "linked"
Index(
    "hat_isin_lineage_linked_old",
    HatIsinLineage.old_instrument_id,
    unique=True,
    postgresql_where=_LINKED,
    sqlite_where=_LINKED,
)
Index(
    "hat_isin_lineage_linked_new",
    HatIsinLineage.new_instrument_id,
    unique=True,
    postgresql_where=_LINKED,
    sqlite_where=_LINKED,
)


class HatLineageOverride(Base):
    """The owner's verdict on one succession (`python -m app.hat.data.lineage link|unlink`, D1):
    `link` links a review case, `unlink` keeps a pair off the chain. Every lineage build applies
    it over the rules' verdict, so it holds across nightly rebuilds."""

    __tablename__ = "hat_lineage_override"
    __table_args__ = (
        UniqueConstraint("old_instrument_id", "new_instrument_id"),
        CheckConstraint("verdict IN ('link', 'unlink')", name="ck_hat_lineage_override_verdict"),
    )

    id = uuid_pk()
    old_instrument_id = fk(INSTRUMENT_ID)
    new_instrument_id = fk(INSTRUMENT_ID)
    verdict = Column(Text, nullable=False)
    note = Column(Text, nullable=False)
    decided_at = Column(Timestamptz, nullable=False, default=now_utc)


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
