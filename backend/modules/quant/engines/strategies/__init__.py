
from .intraday_momentum import IntradayMomentumStrategy
from .intraday_mean_reversion import IntradayMeanReversionStrategy
from .overnight_gap_reversion import OvernightGapReversionStrategy
from .index_mean_reversion import IndexMeanReversionStrategy
from .nifty_strategies import (
    NiftyVolContraction,
    NiftyDarvasBox,
    NiftyMtfTrend,
    NiftyDualMa,
    NiftyVolSpike,
    NiftyTrendEnvelope,
    NiftyRegimeMom,
    NiftyAtrBreak,
    NiftyMaRibbon,
    NiftyMacroBreak
)

# Registry for dynamic loading
STRATEGY_REGISTRY = {
    # Portfolio strategies
    "INTRADAY_MOMENTUM": IntradayMomentumStrategy,
    "INTRADAY_MEAN_REVERSION": IntradayMeanReversionStrategy,
    "OVERNIGHT_GAP": OvernightGapReversionStrategy,
    "INDEX_MEAN_REVERSION": IndexMeanReversionStrategy,
    
    # NIFTY Quant strategies
    "NIFTY_VOL_CONTRACTION": NiftyVolContraction,
    "NIFTY_DARVAS_BOX": NiftyDarvasBox,
    "NIFTY_MTF_TREND": NiftyMtfTrend,
    "NIFTY_DUAL_MA": NiftyDualMa,
    "NIFTY_VOL_SPIKE": NiftyVolSpike,
    "NIFTY_TREND_ENVELOPE": NiftyTrendEnvelope,
    "NIFTY_REGIME_MOM": NiftyRegimeMom,
    "NIFTY_ATR_BREAK": NiftyAtrBreak,
    "NIFTY_MA_RIBBON": NiftyMaRibbon,
    "NIFTY_MACRO_BREAK": NiftyMacroBreak
}
