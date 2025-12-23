"""
Risk State Calculator - Determines portfolio risk state

Outputs: NORMAL, CAUTIOUS, or DEFENSIVE
Uses explicit, hard-coded thresholds only.
"""

from typing import Dict, Any
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class RiskState:
    """Portfolio risk state"""
    state: str  # "NORMAL", "CAUTIOUS", or "DEFENSIVE"
    reason: str
    recommended_action: str

class RiskStateCalculator:
    """
    Calculates portfolio risk state using explicit thresholds.
    
    LOCKED THRESHOLDS (DO NOT MODIFY):
    - DEFENSIVE: DD < -15% OR Vol > 25%
    - CAUTIOUS: DD < -8% OR Vol > 18%
    - NORMAL: Otherwise
    """
    
    # HARD-CODED THRESHOLDS (NON-NEGOTIABLE)
    DD_DEFENSIVE = -0.15
    DD_CAUTIOUS = -0.08
    VOL_DEFENSIVE = 0.25
    VOL_CAUTIOUS = 0.18
    
    def calculate_state(self, portfolio_metrics: Dict[str, Any]) -> RiskState:
        """
        Calculate risk state from portfolio metrics.
        
        Args:
            portfolio_metrics: Dict with 'drawdown' and 'volatility'
            
        Returns:
            RiskState object
        """
        dd = portfolio_metrics.get('drawdown', 0.0)
        vol = portfolio_metrics.get('volatility', 0.0)
        
        # DEFENSIVE STATE
        if dd < self.DD_DEFENSIVE:
            return RiskState(
                state="DEFENSIVE",
                reason=f"Drawdown at {dd:.1%}, below DEFENSIVE threshold of {self.DD_DEFENSIVE:.1%}",
                recommended_action="Scale all weights to 30-40%. Preserve capital."
            )
        
        if vol > self.VOL_DEFENSIVE:
            return RiskState(
                state="DEFENSIVE",
                reason=f"Volatility at {vol:.1%}, above DEFENSIVE threshold of {self.VOL_DEFENSIVE:.1%}",
                recommended_action="Scale all weights to 30-40%. Reduce exposure."
            )
        
        # CAUTIOUS STATE
        if dd < self.DD_CAUTIOUS:
            return RiskState(
                state="CAUTIOUS",
                reason=f"Drawdown at {dd:.1%}, below CAUTIOUS threshold of {self.DD_CAUTIOUS:.1%}",
                recommended_action="Apply weight scaling and correlation penalties."
            )
        
        if vol > self.VOL_CAUTIOUS:
            return RiskState(
                state="CAUTIOUS",
                reason=f"Volatility at {vol:.1%}, above CAUTIOUS threshold of {self.VOL_CAUTIOUS:.1%}",
                recommended_action="Apply weight scaling and correlation penalties."
            )
        
        # NORMAL STATE
        return RiskState(
            state="NORMAL",
            reason="Portfolio metrics within normal ranges",
            recommended_action="No adjustments needed. Maintain current allocations."
        )
    
    def calculate_volatility_regime(self, recent_volatility: float) -> str:
        """
        Classify volatility regime.
        
        Args:
            recent_volatility: Recent realized volatility
            
        Returns:
            "LOW", "MODERATE", or "HIGH"
        """
        if recent_volatility < 0.12:
            return "LOW"
        elif recent_volatility < 0.20:
            return "MODERATE"
        else:
            return "HIGH"
