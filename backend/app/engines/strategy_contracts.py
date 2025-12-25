"""
Strategy-Timeframe-Universe Contracts (HARD-CODED, NON-NEGOTIABLE)

This module enforces strict contracts between strategies, timeframes, and universes.
NO flexibility allowed - these are safety rails against hallucination.
"""

from typing import Dict, Any, List
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class StrategyContract:
    """Immutable contract for a strategy"""
    strategy_id: str
    allowed_universes: List[str]
    timeframe: str  # "5MIN" or "DAILY"
    holding_period: str  # "INTRADAY" or "MULTI_DAY"
    regime: str  # "TREND", "RANGE", "EVENT", "INDEX"
    when_loses: str  # Plain English explanation
    description: str
    
# ============================================================================
# STRATEGY CONTRACTS (ABSOLUTELY NO CHANGES WITHOUT APPROVAL)
# ============================================================================

STRATEGY_CONTRACTS = {
    "INTRADAY_MOMENTUM": StrategyContract(
        strategy_id="INTRADAY_MOMENTUM",
        allowed_universes=["NIFTY100_CORE"],
        timeframe="5MIN",
        holding_period="INTRADAY",
        regime="TREND",
        when_loses="During choppy, range-bound markets with low volume expansion. "
                   "Multiple failed breakouts drain capital through whipsaws.",
        description="Buys breakouts above opening range with volume confirmation. "
                   "Uses ATR for stop-loss placement. Exits by 15:20."
    ),
    
    "INTRADAY_MEAN_REVERSION": StrategyContract(
        strategy_id="INTRADAY_MEAN_REVERSION",
        allowed_universes=["NIFTY100_CORE"],
        timeframe="5MIN",
        holding_period="INTRADAY",
        regime="RANGE",
        when_loses="During strong trending moves with high momentum. "
                   "Trying to fade a trend leads to repeated stop-outs.",
        description="Fades VWAP deviations when RSI is oversold. "
                   "Targets return to VWAP. Intraday only."
    ),
    
    "OVERNIGHT_GAP": StrategyContract(
        strategy_id="OVERNIGHT_GAP",
        allowed_universes=["NIFTY50_CORE"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="EVENT",
        when_loses="When gaps continue in the same direction (trend continuation). "
                   "News-driven gaps can extend for multiple days.",
        description="Fades large opening gaps. Long on gap-down, short on gap-up. "
                   "Holds up to 3 days or until gap fills."
    ),
    
    "INDEX_MEAN_REVERSION": StrategyContract(
        strategy_id="INDEX_MEAN_REVERSION",
        allowed_universes=["NIFTY50_CORE"],
        timeframe="5MIN",
        holding_period="INTRADAY",
        regime="INDEX",
        when_loses="During index breakouts or strong directional moves. "
                   "Indices can trend persistently on macro news.",
        description="Fades index VWAP deviations. Intraday mean reversion "
                   "specific to Nifty/BankNifty indices."
    ),

    # --- NIFTY INSTITUTIONAL STRATEGIES ---

    "NIFTY_VOL_CONTRACTION": StrategyContract(
        strategy_id="NIFTY_VOL_CONTRACTION",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="LOW_VOL_TREND",
        when_loses="False breakouts in choppy, range-bound markets without clear direction.",
        description="Volatility Contraction Breakout - Enters when ATR contracts below historical average and price breaks out with volume."
    ),

    "NIFTY_DARVAS_BOX": StrategyContract(
        strategy_id="NIFTY_DARVAS_BOX",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="STRONG_TREND",
        when_loses="Whipsaws and false breakouts during sideways consolidation phases.",
        description="Darvas Box Breakout - Trades new 50-day highs confirmed by volume surge above 1.1x average."
    ),

    "NIFTY_MTF_TREND": StrategyContract(
        strategy_id="NIFTY_MTF_TREND",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="SUSTAINED_BULL",
        when_loses="Late entries in exhausted trends or sudden reversals in bull markets.",
        description="Multi-Timeframe Trend - Price above 150-day SMA with momentum confirmation for long-term trends."
    ),

    "NIFTY_DUAL_MA": StrategyContract(
        strategy_id="NIFTY_DUAL_MA",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100", "NIFTY_SMALLCAP_100"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="TRENDING",
        when_loses="Sideways markets where moving averages constantly cross over (whipsaw).",
        description="Dual Moving Average Crossover - Classic trend following using 50 EMA and 200 SMA golden cross."
    ),

    "NIFTY_VOL_SPIKE": StrategyContract(
        strategy_id="NIFTY_VOL_SPIKE",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE"],
        timeframe="DAILY",
        holding_period="SWING",
        regime="HIGH_MOMENTUM",
        when_loses="Volume spikes at market tops (climax run) followed by immediate reversal.",
        description="Volume Spike Breakout - Enters on 90-day high with Volume > 1.5x average volume."
    ),

    "NIFTY_TREND_ENVELOPE": StrategyContract(
        strategy_id="NIFTY_TREND_ENVELOPE",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE"],
        timeframe="DAILY",
        holding_period="SWING",
        regime="EXPANSION",
        when_loses="Mean reversion markets where price touches bands but fails to trend.",
        description="Bollinger Breakout - Trades close above upper Bollinger Band (20, 2) indicating expansion."
    ),

    "NIFTY_REGIME_MOM": StrategyContract(
        strategy_id="NIFTY_REGIME_MOM",
        allowed_universes=["NIFTY50_CORE"],
        timeframe="DAILY",
        holding_period="MULTI_DAY",
        regime="BULL_REGIME",
        when_loses="Bear markets or deep corrections where RSI stays oversold despite price drops.",
        description="Regime Momentum - Only trades if price > 200 SMA (Bull Regime) and RSI > 50 (Momentum)."
    ),

    "NIFTY_ATR_BREAK": StrategyContract(
        strategy_id="NIFTY_ATR_BREAK",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
        timeframe="DAILY",
        holding_period="SWING",
        regime="VOL_EXPANSION",
        when_loses="Low volatility drift where price grinds up without breaking ATR bands.",
        description="Keltner-like Breakout - Long if Close > 20 SMA + 2 * ATR."
    ),

    "NIFTY_MA_RIBBON": StrategyContract(
        strategy_id="NIFTY_MA_RIBBON",
        allowed_universes=["NIFTY50_CORE", "NIFTY100_CORE"],
        timeframe="DAILY",
        holding_period="POSITIONAL",
        regime="STRONG_TREND",
        when_loses="Choppy consolidation periods where the ribbon alignment flickers.",
        description="MA Ribbon - Enters when 20 > 50 > 100 > 200 MAs are perfectly aligned."
    ),

    "NIFTY_MACRO_BREAK": StrategyContract(
        strategy_id="NIFTY_MACRO_BREAK",
        allowed_universes=["NIFTY50_CORE"],
        timeframe="DAILY",
        holding_period="LONG_TERM",
        regime="BREAKOUT",
        when_loses="False breakouts of major yearly levels (bull traps).",
        description="Macro Breakout - Enters on new 52-week (252-day) High."
    )
}

# ============================================================================
# VALIDATION LOGIC
# ============================================================================

class ContractViolationError(ValueError):
    """Raised when strategy-universe contracts are violated"""
    pass

def validate_backtest_config(config: Dict[str, Any]) -> bool:
    """
    Enforce strategy-universe compatibility.
    
    Args:
        config: Backtest configuration with 'universe_id' and 'strategies'
        
    Returns:
        True if valid
        
    Raises:
        ContractViolationError: If contract violated
    """
    universe_id = config.get('universe_id')
    strategies = config.get('strategies', [])
    
    if not universe_id:
        raise ContractViolationError("Universe ID is required")
    
    if not strategies:
        raise ContractViolationError("At least one strategy is required")
    
    for strategy_config in strategies:
        strategy_id = strategy_config.get('id')
        
        if strategy_id not in STRATEGY_CONTRACTS:
            raise ContractViolationError(
                f"Unknown strategy: {strategy_id}. "
                f"Valid strategies: {list(STRATEGY_CONTRACTS.keys())}"
            )
        
        contract = STRATEGY_CONTRACTS[strategy_id]
        
        if universe_id not in contract.allowed_universes:
            raise ContractViolationError(
                f"Strategy '{strategy_id}' cannot run on universe '{universe_id}'. "
                f"Allowed universes: {contract.allowed_universes}. "
                f"Reason: {contract.regime} strategies require specific universe properties."
            )
        
        logger.info(
            f"✓ Strategy '{strategy_id}' validated for universe '{universe_id}' "
            f"(timeframe: {contract.timeframe}, holding: {contract.holding_period})"
        )
    
    return True

def get_compatible_strategies(universe_id: str) -> List[str]:
    """
    Get list of strategies compatible with a universe.
    
    Args:
        universe_id: Universe to check
        
    Returns:
        List of compatible strategy IDs
    """
    compatible = []
    for strategy_id, contract in STRATEGY_CONTRACTS.items():
        if universe_id in contract.allowed_universes:
            compatible.append(strategy_id)
    
    return compatible

def get_contract(strategy_id: str) -> StrategyContract:
    """
    Get contract for a strategy.
    
    Args:
        strategy_id: Strategy to look up
        
    Returns:
        StrategyContract
        
    Raises:
        ValueError: If strategy unknown
    """
    if strategy_id not in STRATEGY_CONTRACTS:
        raise ValueError(f"Unknown strategy: {strategy_id}")
    
    return STRATEGY_CONTRACTS[strategy_id]

# ============================================================================
# ANTI-HALLUCINATION ENFORCEMENT
# ============================================================================

# These are FORBIDDEN and will raise immediate errors
FORBIDDEN_FEATURES = {
    "timeframe_selection": "Timeframe is hard-coded per strategy. Use contract timeframe.",
    "parameter_optimization": "Parameters must be set explicitly. No optimization.",
    "strategy_ranking": "Do not rank strategies by metrics. Equal treatment only.",
    "auto_selection": "User must explicitly enable strategies. No auto-selection.",
    "intraday_rebalancing": "Allocator runs EOD only. No intraday weight changes."
}

def enforce_anti_hallucination(feature_name: str):
    """Raise error if forbidden feature is attempted"""
    if feature_name in FORBIDDEN_FEATURES:
        raise ContractViolationError(
            f"FORBIDDEN FEATURE: {feature_name}. "
            f"Reason: {FORBIDDEN_FEATURES[feature_name]}"
        )
