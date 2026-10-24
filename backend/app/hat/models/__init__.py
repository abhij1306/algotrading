"""HAT SQLAlchemy models on the shared `app.base.Base`.

Importing this package registers every HAT table on `Base.metadata`, so `create_all` builds
them (SQLite in tests, Postgres in production).
"""

from .backtest import HatBacktestBucket, HatBacktestRun
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
    HatIsinLineage,
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
    "HatBacktestBucket",
    "HatBacktestRun",
    "HatBot",
    "HatBrokerAccount",
    "HatBrokerSymbol",
    "HatCorporateAction",
    "HatCriterionType",
    "HatExposureState",
    "HatIndustry",
    "HatInstrument",
    "HatInstrumentSymbolHistory",
    "HatIsinLineage",
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
