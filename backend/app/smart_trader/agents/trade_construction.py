"""
Trade Construction Agent
"""
from datetime import datetime
import uuid

from ..models import CompositeSignal, TradeSetup, MarketSnapshot, ConfidenceLevel


class TradeConstructionAgent:
    """
    Constructs trade setups with entry, stop loss, and target prices.
    Only called after confidence classification.
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        
        # Default parameters
        self.atr_sl_multiplier = self.config.get('atr_sl_multiplier', 1.5)
        self.default_rr_ratio = self.config.get('default_rr_ratio', 2.0)
        self.quantity_per_trade = self.config.get('quantity_per_trade', 1)
    
    def construct_trade(
        self,
        composite_signal: CompositeSignal,
        snapshot: MarketSnapshot,
        confidence_level: ConfidenceLevel
    ) -> TradeSetup:
        """
        Construct trade setup from composite signal.
        
        Args:
            composite_signal: CompositeSignal to trade
            snapshot: MarketSnapshot for price data
            confidence_level: Confidence level of signal
            
        Returns:
            TradeSetup object
        """
        # Entry price (current LTP)
        entry_price = snapshot.close
        
        # Calculate stop loss
        stop_loss = self._calculate_stop_loss(
            entry_price,
            composite_signal.direction,
            snapshot
        )
        
        # Calculate target
        target = self._calculate_target(
            entry_price,
            stop_loss,
            composite_signal.direction
        )
        
        # Calculate risk metrics
        risk_per_share = abs(entry_price - stop_loss)
        reward_per_share = abs(target - entry_price)
        risk_reward_ratio = reward_per_share / risk_per_share if risk_per_share > 0 else 0
        
        # Determine quantity (can be adjusted based on confidence)
        quantity = self._calculate_quantity(confidence_level)
        
        # Calculate amounts
        risk_amount = risk_per_share * quantity
        reward_amount = reward_per_share * quantity
        
        # Generate trade ID
        trade_id = f"TRADE_{composite_signal.symbol}_{uuid.uuid4().hex[:8]}"
        
        return TradeSetup(
            trade_id=trade_id,
            composite_signal=composite_signal,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
            quantity=quantity,
            risk_amount=risk_amount,
            reward_amount=reward_amount,
            risk_reward_ratio=risk_reward_ratio,
            setup_method="ATR_BASED",
            created_at=datetime.now()
        )
    
    def _calculate_stop_loss(
        self,
        entry: float,
        direction,
        snapshot: MarketSnapshot
    ) -> float:
        """Calculate stop loss using ATR"""
        atr = snapshot.atr if snapshot.atr else entry * 0.02  # Fallback to 2% of price
        
        if direction.value == "LONG":
            return entry - (atr * self.atr_sl_multiplier)
        else:  # SHORT
            return entry + (atr * self.atr_sl_multiplier)
    
    def _calculate_target(
        self,
        entry: float,
        stop_loss: float,
        direction
    ) -> float:
        """Calculate target using R:R ratio"""
        risk = abs(entry - stop_loss)
        reward = risk * self.default_rr_ratio
        
        if direction.value == "LONG":
            return entry + reward
        else:  # SHORT
            return entry - reward
    
    def _calculate_quantity(self, confidence_level: ConfidenceLevel) -> int:
        """Calculate quantity based on confidence level"""
        # Can adjust quantity based on confidence
        # For now, using fixed quantity
        return self.quantity_per_trade
