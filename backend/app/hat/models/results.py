"""The financial-results store of hat_live (`app/hat/data/results/`): one row per filing version,
append-only, and the growth derived from it point in time."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)

from ...base import Base
from ._types import Money, Ratio, Timestamptz, fk, in_values, now_utc, uuid_pk

INSTRUMENT_ID = "hat_instrument.id"
RESULT_ID = "hat_financial_result.id"

BASES = ("standalone", "consolidated")
FORMATS = (
    "reg33_indas",
    "reg33_banking",
    "reg33_nbfc",
    "integrated_indas",
    "integrated_banking",
    "integrated_nbfc",
    "other",
    "insurance_skipped",
    "secondary",
)
SOURCES = ("nse", "secondary")
PUBLIC_AT_SOURCES = ("announcement", "xbrl", "xbrl_date", "deadline", "observed")


class HatFinancialResult(Base):
    """One version of one company's results for one quarter and basis (standalone or
    consolidated). Never updated: a revision is a new row with the next `version` and
    `supersedes_id` naming the row it replaces (the trigger in `schema.py` refuses UPDATE and
    DELETE). Every figure is for the quarter, in rupees (EPS in rupees a share); the `ytd_`
    figures are the financial year to date from `ytd_period_start`. `public_at` is when the
    figures became public (`data/results/timing.py`); `public_at_estimated` when it is not an
    observed broadcast time. An `insurance_skipped` row is metadata only. A `secondary` row comes
    from the secondary source and fills a quarter NSE does not give."""

    __tablename__ = "hat_financial_result"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "period_end", "basis", "source", "version", name="uq_hat_fin_version"
        ),
        CheckConstraint(in_values("basis", BASES), name="ck_hat_fin_basis"),
        CheckConstraint(in_values("format", FORMATS), name="ck_hat_fin_format"),
        CheckConstraint(in_values("source", SOURCES), name="ck_hat_fin_source"),
        CheckConstraint(in_values("public_at_source", PUBLIC_AT_SOURCES), name="ck_hat_fin_pub"),
        CheckConstraint("version >= 1", name="ck_hat_fin_version"),
    )

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    symbol = Column(Text, nullable=False)
    isin = Column(Text, nullable=False)
    period_start = Column(Date)
    period_end = Column(Date, nullable=False)
    basis = Column(Text, nullable=False)
    format = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    revenue = Column(Money)
    total_income = Column(Money)
    net_profit = Column(Money)
    eps_basic = Column(Money)
    eps_diluted = Column(Money)
    ytd_period_start = Column(Date)
    ytd_revenue = Column(Money)
    ytd_net_profit = Column(Money)
    ytd_eps = Column(Money)
    q4_derived = Column(Boolean, nullable=False, default=False)
    # the listing's period end when it differs from the document's
    listing_period_end = Column(Date)
    period_mismatch = Column(Boolean, nullable=False, default=False)
    public_at = Column(Timestamptz, nullable=False)
    public_at_source = Column(Text, nullable=False)
    public_at_estimated = Column(Boolean, nullable=False, default=False)
    xbrl_at = Column(Timestamptz)
    xbrl_at_estimated = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False)
    supersedes_id = fk(RESULT_ID, nullable=True)
    listing = Column(Text, nullable=False)  # reg33 | integrated | insurance | secondary
    listing_seq = Column(Text, nullable=False)
    source_url = Column(Text, nullable=False)
    sha256 = Column(Text, nullable=False)
    raw_path = Column(Text, nullable=False)
    recorded_at = Column(Timestamptz, nullable=False, default=now_utc)


Index(
    "hat_financial_result_instrument_public",
    HatFinancialResult.instrument_id,
    HatFinancialResult.public_at,
)
Index("hat_financial_result_sha", HatFinancialResult.sha256)


class HatFinancialGrowth(Base):
    """Growth as known when one result row became public (`data/results/derive.py`, rebuilt on
    every run): for the row's (instrument, period_end), the latest version of each quarter public
    at `as_of`, compared with the same quarter a year earlier on one basis (consolidated when both
    quarters have it, else standalone). `unit_suspect` is about the row itself; a suspect row is
    left out of every comparison. `reason` says why a growth value is undefined."""

    __tablename__ = "hat_financial_growth"

    result_id = fk(RESULT_ID, primary_key=True)
    instrument_id = fk(INSTRUMENT_ID)
    period_end = Column(Date, nullable=False)
    as_of = Column(Timestamptz, nullable=False)
    basis = Column(Text)
    current_id = fk(RESULT_ID, nullable=True)
    prior_id = fk(RESULT_ID, nullable=True)
    sales_yoy = Column(Ratio)
    eps_yoy = Column(Ratio)
    turnaround = Column(Boolean)
    unit_suspect = Column(Boolean, nullable=False, default=False)
    reason = Column(Text)
    derived_at = Column(Timestamptz, nullable=False, default=now_utc)


Index(
    "hat_financial_growth_instrument_as_of",
    HatFinancialGrowth.instrument_id,
    HatFinancialGrowth.as_of,
)
