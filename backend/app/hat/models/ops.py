"""Ops tables built in PR 2 (03-schema §7): job runs and quality-gate results."""

from sqlalchemy import CheckConstraint, Column, Date, Integer, Text, UniqueConstraint

from ...base import Base
from ._types import JsonB, Timestamptz, fk, in_values, now_utc, uuid_pk

__all__ = ["JOB_STATUSES", "HatJobRun", "HatQualityCheck"]

JOB_STATUSES = ("running", "succeeded", "failed")


class HatJobRun(Base):
    """One row per (job, trade_date): the unique key makes every job idempotent (T7)."""

    __tablename__ = "hat_job_run"
    __table_args__ = (
        UniqueConstraint("job", "trade_date"),
        CheckConstraint(in_values("status", JOB_STATUSES), name="ck_hat_job_run_status"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_hat_job_run_times"
        ),
    )

    id = uuid_pk()
    job = Column(Text, nullable=False)
    trade_date = Column(Date, nullable=False)
    started_at = Column(Timestamptz, nullable=False, default=now_utc)
    finished_at = Column(Timestamptz)
    status = Column(Text, nullable=False)
    calls = Column(Integer, nullable=False, default=0)
    rows = Column(Integer, nullable=False, default=0)
    # An error code, never raw provider or database text.
    error = Column(Text)


class HatQualityCheck(Base):
    """Gate results that block scans (02 §6)."""

    __tablename__ = "hat_quality_check"
    __table_args__ = (
        CheckConstraint(in_values("status", ("PASS", "FAIL")), name="ck_hat_quality_check_status"),
    )

    job_run_id = fk("hat_job_run.id", primary_key=True)
    check_name = Column(Text, primary_key=True)
    status = Column(Text, nullable=False)
    detail = Column(JsonB, nullable=False)
