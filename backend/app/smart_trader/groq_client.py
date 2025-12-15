"""
Groq API Client for LLM Integration
Uses LLaMA-3 for intelligent market analysis
"""
import os
from typing import Dict, Any, List, Optional
import requests
import json
from datetime import datetime


class GroqClient:
    """Client for Groq API integration"""
    
    def __init__(self, api_key: str = None, model: str = "llama-3.3-70b-versatile", timeout: int = 30):
        self.api_key = api_key or os.getenv('GROQ_API_KEY', '')
        self.model = model
        self.timeout = timeout
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        
        if not self.api_key:
            print("Warning: GROQ_API_KEY not set. LLM features will be disabled.")
    
    def _call_api(self, messages: List[Dict[str, str]], temperature: float = 0.7) -> Optional[str]:
        """Make API call to Groq"""
        if not self.api_key:
            return None
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 1024
        }
        
        try:
            response = requests.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            return result['choices'][0]['message']['content']
            
        except requests.exceptions.RequestException as e:
            print(f"Groq API error: {e}")
            return None
        except (KeyError, IndexError) as e:
            print(f"Groq response parsing error: {e}")
            return None
    
    def analyze_signal(self, signal_data: Dict[str, Any]) -> Optional[str]:
        """
        Analyze a trading signal and provide natural language explanation
        
        Args:
            signal_data: Signal dictionary with symbol, direction, score, reasons etc.
            
        Returns:
            Natural language explanation of the signal
        """
        prompt = f"""You are a quantitative trading analyst. Analyze this trading signal and provide a concise explanation:

Symbol: {signal_data.get('symbol')}
Direction: {signal_data.get('direction')}
Instrument Type: {signal_data.get('instrument_type', 'STOCK')}
Momentum Score: {signal_data.get('momentum_score', 0)}/100
Confidence: {signal_data.get('confidence', 'MEDIUM')}

Reasons:
{chr(10).join(['- ' + r for r in signal_data.get('reasons', [])])}

Entry: ₹{signal_data.get('entry_price', 0):.2f}
Stop Loss: ₹{signal_data.get('stop_loss', 0):.2f}
Target: ₹{signal_data.get('target', 0):.2f}

Provide a brief 2-3 sentence analysis of this signal's quality and key factors to watch."""
        
        messages = [
            {"role": "system", "content": "You are an expert quantitative trading analyst specializing in Indian markets (NSE)."},
            {"role": "user", "content": prompt}
        ]
        
        return self._call_api(messages, temperature=0.5)
    
    def summarize_market(self, scanner_results: Dict[str, Any]) -> Optional[str]:
        """
        Provide market overview summary based on scanner results
        
        Args:
            scanner_results: Dict containing stock and options scanner results
            
        Returns:
            Market summary in natural language
        """
        stock_signals = scanner_results.get('stock_signals', [])
        options_signals = scanner_results.get('options_signals', [])
        
        prompt = f"""You are a quantitative trading analyst. Provide a brief market overview:

**Stocks Scanned**: {scanner_results.get('stocks_scanned', 0)}
**Stock Signals Generated**: {len(stock_signals)}
**Options Signals Generated**: {len(options_signals)}

**Top Stock Signals**:
{chr(10).join([f"- {s.get('symbol')} ({s.get('direction')}) - Score: {s.get('momentum_score', 0)}" for s in stock_signals[:3]])}

**Top Options Signals**:
{chr(10).join([f"- {s.get('symbol')} ({s.get('direction')}) - Score: {s.get('momentum_score', 0)}" for s in options_signals[:3]])}

Provide a 2-3 sentence market summary focusing on current momentum and trading opportunities."""
        
        messages = [
            {"role": "system", "content": "You are an expert market analyst for NSE (Indian stock market)."},
            {"role": "user", "content": prompt}
        ]
        
        return self._call_api(messages, temperature=0.7)
    
    def validate_trade_decision(self, signal: Dict[str, Any], risk_check: Dict[str, Any]) -> Optional[str]:
        """
        Provide validation feedback on a trade decision
        
        Args:
            signal: Trading signal data
            risk_check: Risk management validation result
            
        Returns:
            Validation feedback
        """
        approved = risk_check.get('approved', False)
        rejection_reason = risk_check.get('rejection_reason')
        
        prompt = f"""You are a risk management analyst. Review this trade decision:

**Signal**: {signal.get('symbol')} - {signal.get('direction')}
**Risk Approved**: {"Yes" if approved else "No"}
{f"**Rejection Reason**: {rejection_reason}" if rejection_reason else ""}

**Position Size**: {risk_check.get('qty', 0)} shares
**Risk Amount**: ₹{risk_check.get('risk_amount', 0):.2f}
**Risk/Reward**: 1:{risk_check.get('risk_reward_ratio', 0):.1f}

Provide a brief 1-2 sentence assessment of this trade's risk profile."""
        
        messages = [
            {"role": "system", "content": "You are a risk management expert for algorithmic trading."},
            {"role": "user", "content": prompt}
        ]
        
        return self._call_api(messages, temperature=0.3)
    
    def explain_pattern(self, pattern_name: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Explain a detected technical pattern
        
        Args:
            pattern_name: Name of the pattern (e.g., "Opening Range Breakout")
            context: Context data about the pattern
            
        Returns:
            Pattern explanation
        """
        prompt = f"""Explain this trading pattern in simple terms:

**Pattern**: {pattern_name}
**Context**: {json.dumps(context, indent=2)}

Provide a 2-3 sentence explanation of what this pattern means and what traders should watch for."""
        
        messages = [
            {"role": "system", "content": "You are a technical analysis educator."},
            {"role": "user", "content": prompt}
        ]
        
        return self._call_api(messages, temperature=0.5)


# Global client instance
groq_client = None

def get_groq_client(config: Dict[str, Any] = None) -> GroqClient:
    """Get or create global Groq client instance"""
    global groq_client
    
    if groq_client is None:
        if config:
            groq_client = GroqClient(
                api_key=config.get('api_key'),
                model=config.get('model', 'llama-3.3-70b-versatile'),
                timeout=config.get('timeout', 30)
            )
        else:
            groq_client = GroqClient()
    
    return groq_client
