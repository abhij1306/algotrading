"""HAT SQLAlchemy models: the stock-data tables of hat_live on `app.base.Base`, the trading
and runtime state of hat_state on `app.base.StateBase` (docs/architecture.md, Storage). The
config records are JSON files, not tables (`app.hat.configs`).

Importing this package registers every HAT table on the two metadatas, so `create_all` builds
them (SQLite in tests, Postgres in production).
"""

from .controls import HatExposureState, HatKillSwitch, HatModeChange
from .events import HatEvent
from .fundamentals import HatFundamentalFact, HatFundamentalFiling, HatFundamentalMetric
from .ops import (
    HatBrokerSession,
    HatCommandLog,
    HatDailyBotSnapshot,
    HatHeartbeat,
    HatJobRun,
    HatQualityCheck,
    HatQualityQuarantine,
    HatTelegramCursor,
    HatTelegramMessage,
)
from .portfolio import (
    HatAllocationDaily,
    HatRegimeState,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
)
from .reference import (
    HatAnnouncement,
    HatBrokerSymbol,
    HatCorporateAction,
    HatCorporateActionVersion,
    HatDemergerNotice,
    HatIndexMember,
    HatIndexMembership,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatIsinLineage,
    HatLineageOverride,
    HatMarket,
    HatPriceBand,
    HatTradingCalendar,
)
from .results import HatFinancialGrowth, HatFinancialResult
from .runtime import (
    HatApproval,
    HatFill,
    HatHoldingBaseline,
    HatOrder,
    HatPosition,
    HatSignal,
    HatSignalAnnouncement,
    HatTradeResult,
    HatWatchlistEntry,
)

__all__ = [
    "HatApproval",
    "HatBrokerSession",
    "HatCommandLog",
    "HatDailyBotSnapshot",
    "HatHeartbeat",
    "HatEvent",
    "HatFinancialGrowth",
    "HatFinancialResult",
    "HatFundamentalFact",
    "HatFundamentalFiling",
    "HatFundamentalMetric",
    "HatFill",
    "HatHoldingBaseline",
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
    "HatBrokerSymbol",
    "HatCorporateAction",
    "HatCorporateActionVersion",
    "HatDemergerNotice",
    "HatExposureState",
    "HatIndexMember",
    "HatIndexMembership",
    "HatIndustry",
    "HatInstrument",
    "HatInstrumentSymbolHistory",
    "HatIsinLineage",
    "HatJobRun",
    "HatLineageOverride",
    "HatKillSwitch",
    "HatMarket",
    "HatModeChange",
    "HatPriceBand",
    "HatQualityCheck",
    "HatQualityQuarantine",
    "HatRegimeState",
    "HatTradingCalendar",
    "HatUniverse",
    "HatUniverseMember",
    "HatUniverseVersion",
]
