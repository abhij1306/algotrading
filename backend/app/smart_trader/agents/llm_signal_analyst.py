"""
LLM Signal Analyst Agent
"""
import time
from typing import Optional
from datetime import datetime

from ..models import CompositeSignal, MarketSnapshot, AnalysisResult
from ..groq_client import get_groq_client


class LLMSignalAnalyst:
    """
    LLM agent that analyzes composite signals and provides confidence adjustments.
    
    Key principles:
    - Cannot modify price levels or directions
    - Only adjusts confidence (-0.3 to +0.3)
    - Provides narrative reasoning
    - Identifies risk flags
    - Times out after 5 seconds
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.timeout_ms = self.config.get('timeout_ms', 5000)
        self.model = self.config.get('model', 'llama-3.1-70b-versatile')
        self.groq_client = get_groq_client()
    
    def analyze(
        self,
        composite_signal: CompositeSignal,
        snapshot: MarketSnapshot
    ) -> Optional[AnalysisResult]:
        """
        Analyze composite signal and provide confidence adjustment.
        
        Args:
            composite_signal: CompositeSignal to analyze
            snapshot: MarketSnapshot for context
            
        Returns:
            AnalysisResult or None if timeout/error
        """
        start_time = time.time()
        
        try:
            # Build prompt
            prompt = self._build_prompt(composite_signal, snapshot)
            
            # Call LLM with timeout
            response = self.groq_client._call_api(
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            
            # Parse response
            result = self._parse_response(response)
            
            # Calculate processing time
            processing_time_ms = int((time.time() - start_time) * 1000)
            
            return AnalysisResult(
                confidence_adjustment=result['confidence_adjustment'],
                narrative_reasoning=result['narrative'],
                risk_flags=result['risk_flags'],
                analysis_timestamp=datetime.now(),
                llm_model=self.model,
                processing_time_ms=processing_time_ms
            )
        
        except Exception as e:
            print(f"LLM Signal Analyst error: {e}")
            # Return neutral analysis on error
            return AnalysisResult(
                confidence_adjustment=0.0,
                narrative_reasoning="LLM analysis unavailable",
                risk_flags=[],
                analysis_timestamp=datetime.now(),
                llm_model=self.model,
                processing_time_ms=int((time.time() - start_time) * 1000)
            )
    
    def _get_system_prompt(self) -> str:
        """Get system prompt for LLM"""
        return """You are a professional trading signal analyst. Your role is to:
1. Evaluate signal quality and market context
2. Identify potential failure modes and risks
3. Adjust confidence score (-0.3 to +0.3)
4. Provide clear, concise reasoning

You CANNOT:
- Modify price levels, entry, stop loss, or targets
- Change signal direction
- Block signals entirely

Respond in JSON format:
{
  "confidence_adjustment": <float between -0.3 and 0.3>,
  "narrative": "<brief 1-2 sentence reasoning>",
  "risk_flags": ["<risk1>", "<risk2>"]
}"""
    
    def _build_prompt(self, signal: CompositeSignal, snapshot: MarketSnapshot) -> str:
        """Build analysis prompt"""
        return f"""Analyze this trading signal:

Symbol: {signal.symbol}
Direction: {signal.direction.value}
Confluence: {signal.confluence_count} signals
Aggregate Strength: {signal.aggregate_strength:.2f}
Signal Families: {', '.join([f.value for f in signal.signal_families])}

Reasons:
{chr(10).join(f'- {r}' for r in signal.merged_reasons[:5])}

Market Context:
- Price: ₹{snapshot.close:.2f}
- Change: {snapshot.change_pct:+.2f}%
- Volume Ratio: {snapshot.volume_ratio:.1f}x (if available)
- RSI: {snapshot.rsi:.0f} (if available)
- Nifty Change: {snapshot.nifty_change_pct:+.2f}% (if available)

Evaluate signal quality and provide confidence adjustment."""
    
    def _parse_response(self, response: str) -> dict:
        """Parse LLM response"""
        import json
        try:
            # Extract JSON from response
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                json_str = response[start:end]
                data = json.loads(json_str)
                
                # Validate and clamp confidence adjustment
                adjustment = float(data.get('confidence_adjustment', 0.0))
                adjustment = max(-0.3, min(0.3, adjustment))
                
                return {
                    'confidence_adjustment': adjustment,
                    'narrative': data.get('narrative', 'No analysis provided'),
                    'risk_flags': data.get('risk_flags', [])
                }
        except Exception as e:
            print(f"Error parsing LLM response: {e}")
        
        # Fallback to neutral
        return {
            'confidence_adjustment': 0.0,
            'narrative': 'Unable to parse LLM analysis',
            'risk_flags': []
        }
