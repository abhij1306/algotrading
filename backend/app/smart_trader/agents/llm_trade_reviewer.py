"""
LLM Trade Reviewer Agent
"""
import time
from typing import Optional
from datetime import datetime

from ..models import TradeSetup, ReviewResult
from ..groq_client import get_groq_client


class LLMTradeReviewer:
    """
    LLM agent that reviews trade setups for logic consistency.
    
    Key principles:
    - Can only downgrade confidence or suggest WAIT
    - Cannot approve trades that violate hard risk rules
    - Cannot override RiskAgent decisions
    - Times out after 5 seconds
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.timeout_ms = self.config.get('timeout_ms', 5000)
        self.model = self.config.get('model', 'llama-3.1-70b-versatile')
        self.groq_client = get_groq_client()
    
    def review(self, trade_setup: TradeSetup) -> Optional[ReviewResult]:
        """
        Review trade setup and provide recommendation.
        
        Args:
            trade_setup: TradeSetup to review
            
        Returns:
            ReviewResult or None if timeout/error
        """
        start_time = time.time()
        
        try:
            prompt = self._build_prompt(trade_setup)
            
            response = self.groq_client._call_api(
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2
            )
            
            result = self._parse_response(response)
            processing_time_ms = int((time.time() - start_time) * 1000)
            
            # Ensure adjustment is non-positive
            adjustment = min(0.0, result['confidence_adjustment'])
            
            return ReviewResult(
                recommendation=result['recommendation'],
                reasoning=result['reasoning'],
                confidence_adjustment=adjustment,
                review_timestamp=datetime.now(),
                llm_model=self.model,
                processing_time_ms=processing_time_ms
            )
        
        except Exception as e:
            print(f"LLM Trade Reviewer error: {e}")
            # Default to APPROVE on error (fail-open for review)
            return ReviewResult(
                recommendation="APPROVE",
                reasoning="LLM review unavailable",
                confidence_adjustment=0.0,
                review_timestamp=datetime.now(),
                llm_model=self.model,
                processing_time_ms=int((time.time() - start_time) * 1000)
            )
    
    def _get_system_prompt(self) -> str:
        return """You are a trade setup reviewer. Your role is to:
1. Review trade logic and R:R ratio
2. Check for obvious flaws or risks
3. Recommend: APPROVE, DOWNGRADE (lower confidence), or WAIT

You CANNOT:
- Override hard risk rules
- Approve trades with poor R:R
- Increase confidence (only decrease or keep same)

Respond in JSON format:
{
  "recommendation": "<APPROVE|DOWNGRADE|WAIT>",
  "reasoning": "<brief explanation>",
  "confidence_adjustment": <float <= 0.0>
}"""
    
    def _build_prompt(self, setup: TradeSetup) -> str:
        signal = setup.composite_signal
        return f"""Review this trade setup:

Symbol: {signal.symbol}
Direction: {signal.direction.value}
Entry: ₹{setup.entry_price:.2f}
Stop Loss: ₹{setup.stop_loss:.2f}
Target: ₹{setup.target:.2f}
R:R Ratio: 1:{setup.risk_reward_ratio:.2f}
Quantity: {setup.quantity}

Signal Confluence: {signal.confluence_count}
Signal Strength: {signal.aggregate_strength:.2f}

Reasons:
{chr(10).join(f'- {r}' for r in signal.merged_reasons[:3])}

Is this trade setup logical and well-structured?"""
    
    def _parse_response(self, response: str) -> dict:
        import json
        try:
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                
                recommendation = data.get('recommendation', 'APPROVE').upper()
                if recommendation not in ['APPROVE', 'DOWNGRADE', 'WAIT']:
                    recommendation = 'APPROVE'
                
                adjustment = min(0.0, float(data.get('confidence_adjustment', 0.0)))
                
                return {
                    'recommendation': recommendation,
                    'reasoning': data.get('reasoning', 'No reasoning provided'),
                    'confidence_adjustment': adjustment
                }
        except Exception as e:
            print(f"Error parsing review response: {e}")
        
        return {
            'recommendation': 'APPROVE',
            'reasoning': 'Unable to parse review',
            'confidence_adjustment': 0.0
        }
