"""
Allocator Explainer - Generates plain English explanations for weight changes

This module provides transparent, human-readable reasoning for all allocator decisions.
NO black-box logic allowed.
"""

from typing import Dict, List, Any
from dataclasses import dataclass
from datetime import date
import logging

logger = logging.getLogger(__name__)

@dataclass
class WeightChangeExplanation:
    """Plain English explanation of a weight change"""
    strategy: str
    old_weight: float
    new_weight: float
    delta: float
    reason: str
    recovery_condition: str
    severity: str  # "NORMAL", "CAUTIOUS", "DEFENSIVE"

class AllocatorExplainer:
    """
    Generates plain English explanations for allocator decisions.
    
    RULES:
    1. All reasons must be explicit and traceable
    2. All thresholds must be stated
    3. All recovery conditions must be clear
    4. No jargon or abbreviations
    """
    
    # LOCKED THRESHOLDS (DO NOT CHANGE)
    ACTIVITY_THRESHOLD_LOW = 3  # trades in last 10 sessions
    ACTIVITY_THRESHOLD_ZERO = 0  # auto-suspend
    CORRELATION_SOFT = 0.7
    CORRELATION_HARD = 0.9
    DRAWDOWN_CAUTIOUS = -0.08
    DRAWDOWN_DEFENSIVE = -0.15
    
    def explain_weight_changes(
        self,
        old_weights: Dict[str, float],
        new_weights: Dict[str, float],
        strategy_metrics: Dict[str, Dict[str, Any]],
        current_date: date
    ) -> List[WeightChangeExplanation]:
        """
        Generate explanations for all significant weight changes.
        
        Args:
            old_weights: Previous strategy weights
            new_weights: New strategy weights
            strategy_metrics: Performance metrics for each strategy
            current_date: Date of decision
            
        Returns:
            List of explanations
        """
        explanations = []
        
        for strategy, new_weight in new_weights.items():
            old_weight = old_weights.get(strategy, 0.0)
            delta = new_weight - old_weight
            
            # Only explain significant changes (>5%)
            if abs(delta) > 0.05:
                metrics = strategy_metrics.get(strategy, {})
                
                reason, recovery, severity = self._determine_reason(
                    strategy, metrics, delta
                )
                
                explanation = WeightChangeExplanation(
                    strategy=strategy,
                    old_weight=old_weight,
                    new_weight=new_weight,
                    delta=delta,
                    reason=reason,
                    recovery_condition=recovery,
                    severity=severity
                )
                
                explanations.append(explanation)
                
                logger.info(
                    f"Weight change: {strategy} {old_weight:.1%} → {new_weight:.1%} "
                    f"({delta:+.1%}). Reason: {reason}"
                )
        
        return explanations
    
    def _determine_reason(
        self,
        strategy: str,
        metrics: Dict[str, Any],
        delta: float
    ) -> tuple[str, str, str]:
        """
        Determine reason for weight change.
        
        Returns:
            (reason, recovery_condition, severity)
        """
        drawdown = metrics.get('drawdown', 0.0)
        trades_last_10d = metrics.get('trades_last_10d', 0)
        correlation = metrics.get('correlation_to_portfolio', 0.0)
        
        # DRAWDOWN-BASED
        if drawdown < self.DRAWDOWN_DEFENSIVE:
            return (
                f"Drawdown at {drawdown:.1%}, exceeded DEFENSIVE threshold of {self.DRAWDOWN_DEFENSIVE:.1%}",
                f"Will recover when drawdown improves to {self.DRAWDOWN_CAUTIOUS:.1%}",
                "DEFENSIVE"
            )
        elif drawdown < self.DRAWDOWN_CAUTIOUS:
            return (
                f"Drawdown at {drawdown:.1%}, exceeded CAUTIOUS threshold of {self.DRAWDOWN_CAUTIOUS:.1%}",
                f"Will recover when drawdown improves to -5%",
                "CAUTIOUS"
            )
        
        # ACTIVITY-BASED
        if trades_last_10d == self.ACTIVITY_THRESHOLD_ZERO:
            return (
                f"Zero trades in last 10 sessions (auto-suspend)",
                f"Will recover when activity resumes",
                "CAUTIOUS"
            )
        elif trades_last_10d < self.ACTIVITY_THRESHOLD_LOW:
            return (
                f"Only {trades_last_10d} trades in last 10 sessions (low activity threshold: {self.ACTIVITY_THRESHOLD_LOW})",
                f"Will recover when activity picks up to {self.ACTIVITY_THRESHOLD_LOW}+ trades per 10 days",
                "CAUTIOUS"
            )
        
        # CORRELATION-BASED
        if correlation > self.CORRELATION_HARD:
            return (
                f"Correlation to portfolio at {correlation:.2f}, exceeded hard threshold of {self.CORRELATION_HARD}",
                f"Will recover when correlation drops below {self.CORRELATION_SOFT}",
                "CAUTIOUS"
            )
        elif correlation > self.CORRELATION_SOFT:
            return (
                f"Correlation to portfolio at {correlation:.2f}, soft penalty applied (threshold: {self.CORRELATION_SOFT})",
                f"Will recover when correlation drops below {self.CORRELATION_SOFT}",
                "NORMAL"
            )
        
        # NORMAL OPERATIONS (weight increase)
        if delta > 0:
            return (
                "Strategy performance improved relative to others",
                "No action needed",
                "NORMAL"
            )
        else:
            return (
                "Normal risk-adjusted rebalancing",
                "No action needed",
                "NORMAL"
            )
    
    def generate_summary(
        self,
        explanations: List[WeightChangeExplanation]
    ) -> str:
        """
        Generate a plain English summary of all changes.
        
        Returns:
            Human-readable summary
        """
        if not explanations:
            return "No significant weight changes today. System is protecting capital."
        
        summary_lines = ["WHAT CHANGED TODAY\n"]
        
        for exp in explanations:
            direction = "increased" if exp.delta > 0 else "reduced"
            summary_lines.append(
                f"• {exp.strategy} weight {direction} from {exp.old_weight:.0%} → {exp.new_weight:.0%}\n"
                f"  Reason: {exp.reason}\n"
                f"  {exp.recovery_condition}\n"
            )
        
        # Add overall guidance
        defensive_count = sum(1 for e in explanations if e.severity == "DEFENSIVE")
        if defensive_count > 0:
            summary_lines.append(
                "\n⚠️ DEFENSIVE: Capital protection mode active. "
                "System is scaling down to preserve gains."
            )
        else:
            summary_lines.append(
                "\nNo action needed. System is protecting capital."
            )
        
        return "\n".join(summary_lines)
