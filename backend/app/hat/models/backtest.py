"""`hat_backtest_run` (03-schema §7).

Created in PR 2 rather than PR 3 because strategy and universe versions reference it
(HAT-SCH-104). Trades go to `backtests/run_id=.../trades.parquet` (02 §8).
"""

from sqlalchemy import Column, Date, Text

from ...base import Base
from ._types import JsonB, fk, uuid_pk

__all__ = ["HatBacktestRun"]


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
