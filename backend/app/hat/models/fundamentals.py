"""The fundamentals store of hat_live (`app/hat/data/fundamentals/`, docs/fundamentals.md): every
numeric figure the cached NSE results XBRL and secondary-source pages give, long format, and the
metrics derived from them point in time. Both are projections of the raw cache: each extract and
derive run replaces them."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)

from ...base import Base
from ._types import Ratio, Timestamptz, fk, in_values, now_utc, uuid_pk

INSTRUMENT_ID = "hat_instrument.id"

FUNDAMENTAL_SOURCES = ("nse", "screener")
FUNDAMENTAL_BASES = ("standalone", "consolidated")
FUNDAMENTAL_FAMILIES = ("indas", "nbfc", "bank", "insurer", "other", "screener")
FUNDAMENTAL_PUBLIC_AT = ("announcement", "xbrl", "xbrl_date", "deadline", "observed")
FUNDAMENTAL_STATEMENTS = (
    "pl",
    "bs",
    "cf",
    "segment",
    "other",
    "ratio",
    "shareholding",
    "snapshot",
)
FUNDAMENTAL_PERIOD_KINDS = ("quarter", "ytd", "annual", "instant")


class HatFundamentalFiling(Base):
    """One version of one source document's figures for one period end and basis: an NSE
    results XBRL (`version` in filing order per period end and basis, `public_at` by the results
    timing rules), or the figures one secondary-source page shows for one period end (version 1;
    `public_at` the NSE results of that period where NSE has them, else the regulatory deadline,
    estimated). `family` flags the taxonomy (banks, NBFCs and insurers map fewer items)."""

    __tablename__ = "hat_fundamental_filing"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "period_end",
            "basis",
            "source",
            "version",
            name="uq_hat_fundamental_filing_version",
        ),
        CheckConstraint(in_values("source", FUNDAMENTAL_SOURCES), name="ck_hat_fund_source"),
        CheckConstraint(in_values("basis", FUNDAMENTAL_BASES), name="ck_hat_fund_basis"),
        CheckConstraint(in_values("family", FUNDAMENTAL_FAMILIES), name="ck_hat_fund_family"),
        CheckConstraint(
            in_values("public_at_source", FUNDAMENTAL_PUBLIC_AT), name="ck_hat_fund_public"
        ),
        CheckConstraint("version >= 1", name="ck_hat_fund_version"),
    )

    id = uuid_pk()
    instrument_id = fk(INSTRUMENT_ID)
    symbol = Column(Text, nullable=False)
    isin = Column(Text, nullable=False)
    period_end = Column(Date, nullable=False)
    basis = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    format = Column(Text, nullable=False)  # the XBRL format (`results.xbrl.classify`) or "page"
    family = Column(Text, nullable=False)
    version = Column(Integer, nullable=False)
    public_at = Column(Timestamptz, nullable=False)
    public_at_source = Column(Text, nullable=False)
    public_at_estimated = Column(Boolean, nullable=False, default=False)
    # a secondary-source page shows figures as restated when fetched, not as first published
    point_in_time = Column(Boolean, nullable=False)
    fetched_at = Column(Timestamptz, nullable=False)
    source_url = Column(Text, nullable=False)
    sha256 = Column(Text, nullable=False)
    facts = Column(Integer, nullable=False)
    recorded_at = Column(Timestamptz, nullable=False, default=now_utc)


Index(
    "hat_fundamental_filing_instrument_public",
    HatFundamentalFiling.instrument_id,
    HatFundamentalFiling.public_at,
)


class HatFundamentalFact(Base):
    """One numeric figure of one filing: its statement, period (`period_kind` quarter, year to
    date, year, or `instant` as at `period_end`), the source's own name (`element`: the XBRL
    element, or the page's row label) and context (the XBRL context id, or the page section), the
    canonical `item` where one maps (`data/fundamentals/items.py`), and the value in rupees (EPS
    and prices in rupees a share; `unit` says)."""

    __tablename__ = "hat_fundamental_fact"
    __table_args__ = (
        CheckConstraint(
            in_values("statement", FUNDAMENTAL_STATEMENTS), name="ck_hat_fund_fact_statement"
        ),
        CheckConstraint(
            in_values("period_kind", FUNDAMENTAL_PERIOD_KINDS), name="ck_hat_fund_fact_kind"
        ),
    )

    filing_id = Column(
        Uuid,
        ForeignKey("hat_fundamental_filing.id", ondelete="CASCADE"),
        primary_key=True,
    )
    n = Column(Integer, primary_key=True)
    statement = Column(Text, nullable=False)
    period_kind = Column(Text, nullable=False)
    period_start = Column(Date)
    period_end = Column(Date, nullable=False)
    element = Column(Text, nullable=False)
    context = Column(Text, nullable=False)
    item = Column(Text)
    value = Column(Ratio, nullable=False)
    unit = Column(Text, nullable=False)


Index("hat_fundamental_fact_item", HatFundamentalFact.item, HatFundamentalFact.period_end)


class HatFundamentalMetric(Base):
    """A derived metric of one instrument as known from `as_of` (`data/fundamentals/book.py`):
    a row is written when the metric's value or period changes, so the value at a moment is the
    latest row with `as_of` at or before it. `period_end` is the quarter, year or balance sheet
    the value reflects; valuation metrics use the last close known at `as_of`."""

    __tablename__ = "hat_fundamental_metric"

    instrument_id = fk(INSTRUMENT_ID, primary_key=True)
    metric = Column(Text, primary_key=True)
    as_of = Column(Timestamptz, primary_key=True)
    symbol = Column(Text, nullable=False)
    value = Column(Ratio, nullable=False)
    period_end = Column(Date, nullable=False)
    basis = Column(Text, nullable=False)
    derived_at = Column(Timestamptz, nullable=False, default=now_utc)
