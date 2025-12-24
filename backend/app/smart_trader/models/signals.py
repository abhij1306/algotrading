"""
Signal models for Smart Trader system
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum

from .snapshot import Direction, ConfidenceLevel


class SignalFamily(str, Enum):
    """Signal family categories"""
    MOMENTUM = "MOMENTUM"
    VOLUME = "VOLUME"
    RANGE_EXPANSION = "RANGE_EXPANSION"
    REVERSAL = "REVERSAL"
    INDEX_ALIGNMENT = "INDEX_ALIGNMENT"
    OPTIONS = "OPTIONS"


@dataclass(frozen=True)
class RawSignal:
    """
    Individual signal from a deterministic generator.
    Immutable and contains no entry/SL/target information.
    """
    signal_id: str
    symbol: str
    timeframe: str
    direction: Direction
    signal_family: SignalFamily
    signal_name: str  # Specific signal type, e.g., "EMA_CROSSOVER", "VOLUME_SPIKE"
    strength: float  # 0.0 to 1.0
    features: Dict[str, Any]  # Raw features used for signal detection
    reasons: List[str]  # Human-readable reasons for the signal
    timestamp: datetime
    
    def __post_init__(self):
        """Validate signal strength"""
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Signal strength must be between 0 and 1, got {self.strength}")


@dataclass
class CompositeSignal:
    """
    Aggregated signal from multiple RawSignals.
    Represents confluence of multiple signal types.
    """
    composite_id: str
    symbol: str
    timeframe: str
    direction: Direction
    
    # Aggregation metrics
    confluence_count: int  # Number of signals merged
    aggregate_strength: float  # Weighted average of signal strengths
    signal_families: List[SignalFamily]  # Families contributing to this signal
    signal_names: List[str]  # All signal names merged
    
    # Merged data
    merged_reasons: List[str]  # All reasons from constituent signals
    merged_features: Dict[str, Any]  # Combined features
    
    # Timestamps
    first_signal_time: datetime
    last_signal_time: datetime
    
    # LLM enhancement (added later in flow)
    llm_analysis: Optional['AnalysisResult'] = None
    confidence_level: Optional[ConfidenceLevel] = None
    final_confidence_score: Optional[float] = None


@dataclass
class AnalysisResult:
    """
    Result from LLM Signal Analyst.
    Enhances signal understanding without modifying core signal data.
    """
    confidence_adjustment: float  # -0.3 to +0.3
    narrative_reasoning: str  # Human-readable explanation
    risk_flags: List[str]  # Warnings like "Low liquidity", "Choppy market"
    analysis_timestamp: datetime
    llm_model: str
    processing_time_ms: int


@dataclass
class TradeSetup:
    """
    Final trade setup with entry, stop loss, and target.
    Created only after confidence classification.
    """
    trade_id: str
    composite_signal: CompositeSignal
    
    # Trade parameters
    entry_price: float
    stop_loss: float
    target: float
    quantity: int
    
    # Risk metrics
    risk_amount: float  # Amount at risk (entry - SL) * quantity
    reward_amount: float  # Potential reward (target - entry) * quantity
    risk_reward_ratio: float
    
    # Metadata
    setup_method: str  # "ATR_BASED", "RR_BASED", "SUPPORT_RESISTANCE"
    created_at: datetime
    
    # LLM review (optional)
    llm_review: Optional['ReviewResult'] = None


@dataclass
class ReviewResult:
    """
    Result from LLM Trade Reviewer.
    Can only downgrade confidence or suggest WAIT.
    """
    recommendation: str  # "APPROVE", "DOWNGRADE", "WAIT"
    reasoning: str
    confidence_adjustment: float  # Only negative or 0
    review_timestamp: datetime
    llm_model: str
    processing_time_ms: int
    
    def __post_init__(self):
        """Validate that adjustment is non-positive"""
        if self.confidence_adjustment > 0:
            raise ValueError("LLM Trade Reviewer can only downgrade confidence (adjustment must be <= 0)")


@dataclass
class RiskCheckResult:
    """
    Result from Risk Agent hard limit checks.
    Cannot be overridden by LLM.
    """
    approved: bool
    reasons: List[str]  # Reasons for approval/rejection
    limits_checked: Dict[str, bool]  # Which limits were checked and passed
    timestamp: datetime
