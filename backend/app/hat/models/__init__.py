"""HAT SQLAlchemy models on the shared `app.base.Base` (03-schema §10).

Importing this package registers every HAT table on `Base.metadata`, so `create_all` builds
them (SQLite in tests, Postgres in production).
"""

from .backtest import HatBacktestRun
from .events import HatEvent
from .ops import (
    HatBrokerSession,
    HatCommandLog,
    HatDailyBotSnapshot,
    HatJobRun,
    HatQualityCheck,
    HatQualityQuarantine,
    HatTelegramCursor,
    HatTelegramMessage,
)
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
from .runtime import (
    HatApproval,
    HatFill,
    HatOrder,
    HatPosition,
    HatSignal,
    HatSignalAnnouncement,
    HatTradeResult,
    HatWatchlistEntry,
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
    "HatApproval",
    "HatBrokerSession",
    "HatCommandLog",
    "HatDailyBotSnapshot",
    "HatEvent",
    "HatFill",
    "HatOrder",
    "HatPosition",
    "HatSignal",
    "HatSignalAnnouncement",
    "HatTelegramCursor",
    "HatTelegramMessage",
    "HatTradeResult",
    "HatWatchlistEntry",
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
    "HatQualityQuarantine",
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
