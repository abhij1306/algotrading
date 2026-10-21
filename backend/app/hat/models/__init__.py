"""HAT SQLAlchemy models on the shared `app.base.Base` (03-schema §10).

Importing this package registers every HAT table on `Base.metadata`, so `create_all` builds
them (SQLite in tests, Postgres in production).
"""

from .backtest import HatBacktestRun
from .ops import HatJobRun, HatQualityCheck
from .portfolio import (
    HatAllocationDaily,
    HatFundamental,
    HatRegimeModelVersion,
    HatRegimeState,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
)
from .reference import (
    HatAnnouncement,
    HatBrokerSymbol,
    HatCorporateAction,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatMarket,
    HatPriceBand,
    HatTradingCalendar,
)
from .strategy import (
    HatBot,
    HatBrokerAccount,
    HatCriterionType,
    HatExposureState,
    HatKillSwitch,
    HatModeChange,
    HatRiskPolicyVersion,
    HatStrategy,
    HatStrategyVersion,
)

__all__ = [
    "HatAllocationDaily",
    "HatAnnouncement",
    "HatBacktestRun",
    "HatBot",
    "HatBrokerAccount",
    "HatBrokerSymbol",
    "HatCorporateAction",
    "HatCriterionType",
    "HatExposureState",
    "HatFundamental",
    "HatIndustry",
    "HatInstrument",
    "HatInstrumentSymbolHistory",
    "HatJobRun",
    "HatKillSwitch",
    "HatMarket",
    "HatModeChange",
    "HatPriceBand",
    "HatQualityCheck",
    "HatRegimeModelVersion",
    "HatRegimeState",
    "HatRiskPolicyVersion",
    "HatStrategy",
    "HatTradingCalendar",
    "HatStrategyVersion",
    "HatUniverse",
    "HatUniverseMember",
    "HatUniverseVersion",
]
