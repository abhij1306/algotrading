"""
LLM Risk Optimizer - AI-powered risk management and position sizing
"""
from typing import Dict, Any, Optional, List
from .llm_client import get_llm_client
import json


class LLMRiskAgent:
    """
    Uses LLM to optimize risk parameters dynamically
    
    Features:
    - Dynamic stop-loss calculation based on ATR and volatility
    - Multiple take-profit targets (T1, T2, T3)
    - Optimal position sizing based on account risk
    - Risk:reward ratio optimization
    - Volatility-adjusted risk management
    """
    
    def __init__(self, provider: str = "groq"):
        """Initialize LLM Risk Optimizer"""
        self.llm_client = get_llm_client(provider=provider)
    
    async def optimize_risk_params(
        self,
        signal: Dict[str, Any],
        account: Dict[str, Any],
        market_conditions: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Optimize risk parameters for a trade
        
        Args:
            signal: Trading signal with entry price, symbol, indicators
            account: Account info (capital, max_risk_pct, etc.)
            market_conditions: Current market volatility, trend, etc.
            
        Returns:
            Optimized risk parameters
        """
        # Default market conditions
        if market_conditions is None:
            market_conditions = {
                "volatility": "medium",
                "trend": "neutral",
                "vix": 15.0
            }
        
        # Build optimization prompt
        prompt = self._build_risk_prompt(signal, account, market_conditions)
        
        try:
            # Get LLM optimization
            risk_params = await self.llm_client.generate_json(
                prompt=prompt,
                temperature=0.2,  # Lower temperature for consistent risk management
                max_tokens=1500
            )
            
            # Validate and structure response
            return {
                "stop_loss": {
                    "price": risk_params.get("stop_loss_price"),
                    "percentage": risk_params.get("stop_loss_pct"),
                    "reasoning": risk_params.get("sl_reasoning", "")
                },
                "take_profit": {
                    "t1": risk_params.get("take_profit_t1"),
                    "t2": risk_params.get("take_profit_t2"),
                    "t3": risk_params.get("take_profit_t3"),
                    "reasoning": risk_params.get("tp_reasoning", "")
                },
                "position_size": {
                    "quantity": risk_params.get("position_quantity"),
                    "capital_allocation": risk_params.get("capital_allocation"),
                    "reasoning": risk_params.get("sizing_reasoning", "")
                },
                "risk_reward": risk_params.get("risk_reward_ratio"),
                "max_loss": risk_params.get("max_loss_amount"),
                "expected_profit": risk_params.get("expected_profit"),
                "confidence": risk_params.get("confidence_level", "medium"),
                "optimized": True
            }
        
        except Exception as e:
            print(f"LLM Risk Optimization failed: {str(e)}")
            # Return basic risk params if LLM fails
            return self._fallback_risk_params(signal, account)
    
    def _build_risk_prompt(
        self,
        signal: Dict[str, Any],
        account: Dict[str, Any],
        market_conditions: Dict[str, Any]
    ) -> str:
        """Build comprehensive risk optimization prompt"""
        
        symbol = signal.get("symbol", "UNKNOWN")
        entry_price = signal.get("entry_price", 0)
        atr = signal.get("atr", entry_price * 0.02)  # Default 2% if not available
        volatility = signal.get("volatility", "medium")
        
        # Account details
        capital = account.get("capital", 100000)
        max_risk_pct = account.get("max_risk_pct", 2.0)
        
        # Support/Resistance levels if available
        support = signal.get("support_level", entry_price * 0.95)
        resistance = signal.get("resistance_level", entry_price * 1.05)
        
        prompt = f"""Optimize risk management parameters for this trade:

**TRADE DETAILS:**
Symbol: {symbol}
Entry Price: ₹{entry_price}
Signal Type: {signal.get('signal_type', 'LONG')}

**TECHNICAL INDICATORS:**
- ATR (Average True Range): ₹{atr}
- Volatility: {volatility}
- Support Level: ₹{support}
- Resistance Level: ₹{resistance}
- RSI: {signal.get('rsi', 'N/A')}

**ACCOUNT INFORMATION:**
- Total Capital: ₹{capital:,.2f}
- Max Risk per Trade: {max_risk_pct}%
- Max Risk Amount: ₹{capital * max_risk_pct / 100:,.2f}

**MARKET CONDITIONS:**
- Market Volatility: {market_conditions.get('volatility', 'medium')}
- Overall Trend: {market_conditions.get('trend', 'neutral')}
- VIX Level: {market_conditions.get('vix', 15.0)}

**OPTIMIZATION REQUIRED:**

Calculate optimal risk parameters considering:

1. **Stop Loss:**
   - Use ATR-based calculation (typically 1.5-2x ATR)
   - Consider support levels
   - Account for current volatility
   - Provide both price and percentage

2. **Take Profit Targets:**
   - T1 (First target): Conservative, high probability
   - T2 (Second target): Moderate, good R:R
   - T3 (Third target): Aggressive, best R:R
   - Use resistance levels and ATR multiples

3. **Position Sizing:**
   - Calculate quantity based on max risk amount
   - Ensure position doesn't exceed risk limits
   - Consider capital allocation percentage

4. **Risk:Reward Ratio:**
   - Calculate for each target
   - Minimum acceptable: 1.5:1
   - Optimal: 2:1 or better

**IMPORTANT RULES:**
- Stop loss must be below entry for LONG (above for SHORT)
- Take profits must be above entry for LONG (below for SHORT)
- Position size must not exceed max risk amount
- Be conservative in high volatility environments
- Consider partial profit booking at T1 and T2

**OUTPUT FORMAT (JSON):**
{{
    "stop_loss_price": 1450.00,
    "stop_loss_pct": 3.33,
    "sl_reasoning": "1.5x ATR below entry, just below support at 1455",
    
    "take_profit_t1": 1530.00,
    "take_profit_t2": 1580.00,
    "take_profit_t3": 1650.00,
    "tp_reasoning": "T1 at resistance, T2 at 2x ATR, T3 at 3x ATR",
    
    "position_quantity": 40,
    "capital_allocation": 5.0,
    "sizing_reasoning": "40 shares = ₹2000 risk (2% of capital)",
    
    "risk_reward_ratio": 2.4,
    "max_loss_amount": 2000.00,
    "expected_profit": 4800.00,
    "confidence_level": "high"
}}
"""
        return prompt
    
    def _fallback_risk_params(
        self,
        signal: Dict[str, Any],
        account: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback risk parameters if LLM fails"""
        
        entry_price = signal.get("entry_price", 0)
        atr = signal.get("atr", entry_price * 0.02)
        capital = account.get("capital", 100000)
        max_risk_pct = account.get("max_risk_pct", 2.0)
        
        # Simple ATR-based calculations
        stop_loss_price = entry_price - (1.5 * atr)
        stop_loss_pct = ((entry_price - stop_loss_price) / entry_price) * 100
        
        # Risk-based position sizing
        max_risk_amount = capital * (max_risk_pct / 100)
        risk_per_share = entry_price - stop_loss_price
        position_quantity = int(max_risk_amount / risk_per_share) if risk_per_share > 0 else 1
        
        # Take profit targets
        tp_t1 = entry_price + (1.5 * atr)
        tp_t2 = entry_price + (2.5 * atr)
        tp_t3 = entry_price + (3.5 * atr)
        
        return {
            "stop_loss": {
                "price": round(stop_loss_price, 2),
                "percentage": round(stop_loss_pct, 2),
                "reasoning": "Fallback: 1.5x ATR below entry"
            },
            "take_profit": {
                "t1": round(tp_t1, 2),
                "t2": round(tp_t2, 2),
                "t3": round(tp_t3, 2),
                "reasoning": "Fallback: ATR-based targets"
            },
            "position_size": {
                "quantity": position_quantity,
                "capital_allocation": round((position_quantity * entry_price / capital) * 100, 2),
                "reasoning": "Fallback: Risk-based sizing"
            },
            "risk_reward": round((tp_t2 - entry_price) / (entry_price - stop_loss_price), 2),
            "max_loss": round(max_risk_amount, 2),
            "expected_profit": round((tp_t2 - entry_price) * position_quantity, 2),
            "confidence": "low",
            "optimized": False
        }
    
    async def adjust_for_volatility(
        self,
        base_params: Dict[str, Any],
        current_vix: float
    ) -> Dict[str, Any]:
        """
        Adjust risk parameters based on market volatility
        
        Args:
            base_params: Base risk parameters
            current_vix: Current VIX level
            
        Returns:
            Adjusted risk parameters
        """
        prompt = f"""Adjust these risk parameters for current market volatility:

**CURRENT PARAMETERS:**
Stop Loss: {base_params.get('stop_loss', {}).get('percentage', 0)}%
Position Size: {base_params.get('position_size', {}).get('quantity', 0)} shares

**MARKET VOLATILITY:**
VIX Level: {current_vix}
Interpretation: {"High" if current_vix > 20 else "Medium" if current_vix > 15 else "Low"}

**ADJUSTMENT RULES:**
- High VIX (>20): Widen stops by 20-30%, reduce position size by 20-30%
- Medium VIX (15-20): Widen stops by 10-15%, reduce position size by 10-15%
- Low VIX (<15): Keep parameters as is or tighten slightly

Provide adjusted parameters in JSON format:
{{
    "adjusted_stop_loss_pct": 4.5,
    "adjusted_position_quantity": 30,
    "adjustment_reasoning": "High VIX: widened SL by 25%, reduced size by 25%"
}}
"""
        
        try:
            adjustment = await self.llm_client.generate_json(prompt=prompt)
            return {
                **base_params,
                "volatility_adjusted": True,
                "adjustments": adjustment
            }
        except:
            return base_params
