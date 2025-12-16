"""
Confidence Engine - Computes and adjusts signal confidence
"""
from typing import Optional

from ..models import CompositeSignal, AnalysisResult, ConfidenceLevel


class ConfidenceEngine:
    """
    Computes signal confidence deterministically and applies LLM adjustments.
    
    Flow:
    1. Compute base confidence from signal metrics
    2. Apply LLM adjustment (if available)
    3. Map to confidence level (LOW/MEDIUM/HIGH)
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        
        # Thresholds for confidence levels
        self.low_threshold = self.config.get('low_threshold', 0.4)
        self.high_threshold = self.config.get('high_threshold', 0.7)
    
    def compute_confidence(
        self,
        composite_signal: CompositeSignal,
        llm_analysis: Optional[AnalysisResult] = None
    ) -> tuple[float, ConfidenceLevel]:
        """
        Compute final confidence score and level.
        
        Args:
            composite_signal: CompositeSignal to evaluate
            llm_analysis: Optional LLM analysis result
            
        Returns:
            Tuple of (confidence_score, confidence_level)
        """
        # Step 1: Compute base confidence deterministically
        base_confidence = self._compute_base_confidence(composite_signal)
        
        # Step 2: Apply LLM adjustment if available
        final_confidence = base_confidence
        if llm_analysis:
            final_confidence += llm_analysis.confidence_adjustment
            final_confidence = max(0.0, min(1.0, final_confidence))  # Clamp to [0, 1]
        
        # Step 3: Map to confidence level
        confidence_level = self._map_to_level(final_confidence)
        
        return final_confidence, confidence_level
    
    def _compute_base_confidence(self, signal: CompositeSignal) -> float:
        """
        Compute base confidence from signal metrics.
        
        Factors:
        - Confluence count (more signals = higher confidence)
        - Aggregate strength from generators
        - Signal family diversity
        """
        # Confluence component (0.0 - 0.4)
        # 1 signal = 0.1, 2 = 0.2, 3 = 0.3, 4+ = 0.4
        confluence_score = min(signal.confluence_count * 0.1, 0.4)
        
        # Strength component (0.0 - 0.4)
        # Direct mapping of aggregate strength
        strength_score = signal.aggregate_strength * 0.4
        
        # Diversity component (0.0 - 0.2)
        # More signal families = higher confidence
        family_count = len(signal.signal_families)
        diversity_score = min(family_count * 0.05, 0.2)
        
        # Combine components
        base_confidence = confluence_score + strength_score + diversity_score
        
        return min(base_confidence, 1.0)
    
    def _map_to_level(self, confidence_score: float) -> ConfidenceLevel:
        """
        Map confidence score to discrete level.
        
        Args:
            confidence_score: Float between 0.0 and 1.0
            
        Returns:
            ConfidenceLevel enum
        """
        if confidence_score >= self.high_threshold:
            return ConfidenceLevel.HIGH
        elif confidence_score >= self.low_threshold:
            return ConfidenceLevel.MEDIUM
        else:
            return ConfidenceLevel.LOW
    
    def get_confidence_description(self, level: ConfidenceLevel) -> str:
        """Get human-readable description of confidence level"""
        descriptions = {
            ConfidenceLevel.HIGH: "High confidence - Multiple strong signals aligned",
            ConfidenceLevel.MEDIUM: "Medium confidence - Moderate signal strength",
            ConfidenceLevel.LOW: "Low confidence - Weak or single signal"
        }
        return descriptions.get(level, "Unknown confidence level")
