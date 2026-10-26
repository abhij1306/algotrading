"""`hat_backtest_run`.

Created in PR 2 rather than PR 3 because strategy and universe versions reference it
(HAT-SCH-104). No backtest writes it any more: runs are folders under
`HAT_DATA_ROOT/backtests/runs/` (docs/decisions.md, Backtest results are files).
"""

from sqlalchemy import CheckConstraint, Column, Date, Integer, Text

from ...base import Base
from ._types import JsonB, Ratio, fk, uuid_pk

__all__ = ["HatBacktestBucket", "HatBacktestRun"]


class HatBacktestRun(Base):
    __tablename__ = "hat_backtest_run"

    id = uuid_pk()
    strategy_version_id = fk("hat_strategy_version.id")
    risk_policy_version_id = fk("hat_risk_policy_version.id")
    # strategy x universe; every run counts as a trial of its family (HAT-SCH-071).
    family_key = Column(Text, nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    data_snapshot = Column(JsonB, nullable=False)
    status = Column(Text, nullable=False)
    metrics = Column(JsonB, nullable=False)


class HatBacktestBucket(Base):
    """A run's closed trades in one (playbook, setup-score band): the evidence a signal's
    confidence label reads (D132, PR 71). Written once, when the run completes."""

    __tablename__ = "hat_backtest_bucket"
    __table_args__ = (CheckConstraint("trades > 0", name="ck_hat_backtest_bucket_trades"),)

    run_id = fk("hat_backtest_run.id", primary_key=True)
    playbook = Column(Text, primary_key=True)
    band = Column(Text, primary_key=True)
    trades = Column(Integer, nullable=False)
    mean_r = Column(Ratio, nullable=False)
    # the 90% interval of the mean R (normal); null below two trades
    ci90_low = Column(Ratio)
    ci90_high = Column(Ratio)
    win_rate = Column(Ratio, nullable=False)
