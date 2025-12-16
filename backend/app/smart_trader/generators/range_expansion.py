"""
Range expansion signal generator
"""
from typing import List
from .base import SignalGenerator
from ..models import MarketSnapshot, RawSignal, Direction, SignalFamily


class RangeExpansionGenerator(SignalGenerator):
    """
    Generates signals based on range expansion and consolidation breakouts.
    
    Signals:
    - ATR_EXPANSION: ATR expanding significantly
    - RANGE_BREAKOUT: Breaking out of consolidation
    """
    
    @property
    def signal_family(self) -> str:
        return SignalFamily.RANGE_EXPANSION.value
    
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """Generate range expansion signals"""
        signals = []
        
        atr_expansion = self._check_atr_expansion(snapshot)
        if atr_expansion:
            signals.append(atr_expansion)
        
        range_breakout = self._check_range_breakout(snapshot)
        if range_breakout:
            signals.append(range_breakout)
        
        return signals
    
    def _check_atr_expansion(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for ATR expansion"""
        if not snapshot.atr or not snapshot.volatility:
            return None
        
        # ATR expansion: Current range significantly above ATR
        current_range = snapshot.high - snapshot.low
        atr = snapshot.atr
        expansion_ratio = current_range / atr if atr > 0 else 0
        
        if expansion_ratio >= 1.3:  # 30% above ATR
            strength = min((expansion_ratio - 1.0) / 0.5, 1.0)
            
            direction = Direction.LONG if snapshot.change_pct > 0 else Direction.SHORT
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "ATR_EXPANSION"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=direction,
                signal_family=SignalFamily.RANGE_EXPANSION,
                signal_name="ATR_EXPANSION",
                strength=strength,
                features={
                    "current_range": current_range,
                    "atr": atr,
                    "expansion_ratio": expansion_ratio
                },
                reasons=[
                    f"ATR expansion: {(expansion_ratio - 1) * 100:.0f}% above normal",
                    f"Current range: ₹{current_range:.2f}",
                    "Increased volatility"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
    
    def _check_range_breakout(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for consolidation breakout"""
        if not snapshot.day_high or not snapshot.day_low or not snapshot.prev_close:
            return None
        
        # Check if breaking day high/low
        breaking_high = snapshot.close > snapshot.day_high * 0.998  # Within 0.2%
        breaking_low = snapshot.close < snapshot.day_low * 1.002
        
        if breaking_high or breaking_low:
            # Calculate strength based on how far beyond the level
            if breaking_high:
                breakout_pct = ((snapshot.close - snapshot.day_high) / snapshot.day_high) * 100
                strength = min(abs(breakout_pct) * 2.0, 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "RANGE_BREAKOUT_HIGH"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.LONG,
                    signal_family=SignalFamily.RANGE_EXPANSION,
                    signal_name="RANGE_BREAKOUT",
                    strength=strength,
                    features={
                        "day_high": snapshot.day_high,
                        "close": snapshot.close,
                        "breakout_pct": breakout_pct
                    },
                    reasons=[
                        f"Breaking day high: ₹{snapshot.day_high:.2f}",
                        f"Current price: ₹{snapshot.close:.2f}",
                        "Bullish breakout"
                    ],
                    timestamp=snapshot.timestamp
                )
            
            else:  # breaking_low
                breakout_pct = ((snapshot.day_low - snapshot.close) / snapshot.day_low) * 100
                strength = min(abs(breakout_pct) * 2.0, 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "RANGE_BREAKOUT_LOW"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.SHORT,
                    signal_family=SignalFamily.RANGE_EXPANSION,
                    signal_name="RANGE_BREAKOUT",
                    strength=strength,
                    features={
                        "day_low": snapshot.day_low,
                        "close": snapshot.close,
                        "breakout_pct": breakout_pct
                    },
                    reasons=[
                        f"Breaking day low: ₹{snapshot.day_low:.2f}",
                        f"Current price: ₹{snapshot.close:.2f}",
                        "Bearish breakdown"
                    ],
                    timestamp=snapshot.timestamp
                )
        
        return None
