"""
Reversal pattern signal generator
"""
from typing import List
from .base import SignalGenerator
from ..models import MarketSnapshot, RawSignal, Direction, SignalFamily


class ReversalGenerator(SignalGenerator):
    """
    Generates reversal signals based on RSI and candlestick patterns.
    
    Signals:
    - RSI_OVERSOLD: RSI in oversold territory
    - RSI_OVERBOUGHT: RSI in overbought territory
    - BULLISH_REVERSAL: Bullish candlestick pattern
    - BEARISH_REVERSAL: Bearish candlestick pattern
    """
    
    @property
    def signal_family(self) -> str:
        return SignalFamily.REVERSAL.value
    
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """Generate reversal signals"""
        signals = []
        
        rsi_signal = self._check_rsi_extremes(snapshot)
        if rsi_signal:
            signals.append(rsi_signal)
        
        candlestick_signal = self._check_candlestick_patterns(snapshot)
        if candlestick_signal:
            signals.append(candlestick_signal)
        
        return signals
    
    def _check_rsi_extremes(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for RSI oversold/overbought conditions"""
        if not snapshot.rsi:
            return None
        
        rsi = snapshot.rsi
        
        # Oversold: RSI < 30
        if rsi < 30:
            strength = (30 - rsi) / 30  # Stronger as RSI gets lower
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "RSI_OVERSOLD"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.LONG,
                signal_family=SignalFamily.REVERSAL,
                signal_name="RSI_OVERSOLD",
                strength=strength,
                features={"rsi": rsi},
                reasons=[
                    f"RSI oversold at {rsi:.1f}",
                    "Potential bullish reversal"
                ],
                timestamp=snapshot.timestamp
            )
        
        # Overbought: RSI > 70
        elif rsi > 70:
            strength = (rsi - 70) / 30  # Stronger as RSI gets higher
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "RSI_OVERBOUGHT"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.SHORT,
                signal_family=SignalFamily.REVERSAL,
                signal_name="RSI_OVERBOUGHT",
                strength=strength,
                features={"rsi": rsi},
                reasons=[
                    f"RSI overbought at {rsi:.1f}",
                    "Potential bearish reversal"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
    
    def _check_candlestick_patterns(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for reversal candlestick patterns"""
        if not snapshot.prev_close:
            return None
        
        body = abs(snapshot.close - snapshot.open)
        total_range = snapshot.high - snapshot.low
        
        if total_range == 0:
            return None
        
        body_ratio = body / total_range
        
        # Bullish Engulfing / Hammer
        if snapshot.close > snapshot.open:  # Bullish candle
            lower_wick = snapshot.open - snapshot.low
            upper_wick = snapshot.high - snapshot.close
            
            # Hammer pattern: Long lower wick, small upper wick
            if lower_wick > body * 2 and upper_wick < body * 0.5:
                strength = min(lower_wick / body / 3.0, 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "BULLISH_HAMMER"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.LONG,
                    signal_family=SignalFamily.REVERSAL,
                    signal_name="BULLISH_REVERSAL",
                    strength=strength,
                    features={
                        "pattern": "hammer",
                        "body_ratio": body_ratio,
                        "lower_wick_ratio": lower_wick / body
                    },
                    reasons=[
                        "Bullish hammer pattern detected",
                        f"Long lower wick: {lower_wick / body:.1f}x body",
                        "Potential reversal from support"
                    ],
                    timestamp=snapshot.timestamp
                )
        
        # Bearish Engulfing / Shooting Star
        elif snapshot.close < snapshot.open:  # Bearish candle
            lower_wick = snapshot.close - snapshot.low
            upper_wick = snapshot.high - snapshot.open
            
            # Shooting star: Long upper wick, small lower wick
            if upper_wick > body * 2 and lower_wick < body * 0.5:
                strength = min(upper_wick / body / 3.0, 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "BEARISH_STAR"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.SHORT,
                    signal_family=SignalFamily.REVERSAL,
                    signal_name="BEARISH_REVERSAL",
                    strength=strength,
                    features={
                        "pattern": "shooting_star",
                        "body_ratio": body_ratio,
                        "upper_wick_ratio": upper_wick / body
                    },
                    reasons=[
                        "Bearish shooting star pattern detected",
                        f"Long upper wick: {upper_wick / body:.1f}x body",
                        "Potential reversal from resistance"
                    ],
                    timestamp=snapshot.timestamp
                )
        
        return None
