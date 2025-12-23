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
        allowed_universes=["NIFTY100_LIQUID_50"],
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
        allowed_universes=["NIFTY100_MEAN_REV"],
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
        allowed_universes=["NIFTY50_ONLY"],
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
        allowed_universes=["NIFTY-INDEX", "BANKNIFTY-INDEX"],
        timeframe="5MIN",
        holding_period="INTRADAY",
        regime="INDEX",
        when_loses="During index breakouts or strong directional moves. "
                   "Indices can trend persistently on macro news.",
        description="Fades index VWAP deviations. Intraday mean reversion "
                   "specific to Nifty/BankNifty indices."
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
