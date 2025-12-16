"""
Momentum-based signal generator
"""
from typing import List
from datetime import datetime

from .base import SignalGenerator
from ..models import MarketSnapshot, RawSignal, Direction, SignalFamily


class MomentumGenerator(SignalGenerator):
    """
    Generates momentum signals based on EMA crossovers and price-EMA relationships.
    
    Signals:
    - EMA_CROSSOVER: EMA 9 crosses EMA 21
    - PRICE_ABOVE_EMA: Price significantly above key EMA
    - STRONG_TREND: Multiple EMAs aligned
    """
    
    @property
    def signal_family(self) -> str:
        return SignalFamily.MOMENTUM.value
    
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """Generate momentum signals from snapshot"""
        signals = []
        
        # Check EMA crossover signal
        crossover_signal = self._check_ema_crossover(snapshot)
        if crossover_signal:
            signals.append(crossover_signal)
        
        # Check price-EMA relationship
        price_ema_signal = self._check_price_ema_relationship(snapshot)
        if price_ema_signal:
            signals.append(price_ema_signal)
        
        # Check trend alignment
        trend_signal = self._check_trend_alignment(snapshot)
        if trend_signal:
            signals.append(trend_signal)
        
        return signals
    
    def _check_ema_crossover(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for EMA 9/21 crossover"""
        if not self._check_required_indicators(snapshot, ['ema_9', 'ema_21']):
            return None
        
        ema_9 = snapshot.ema_9
        ema_21 = snapshot.ema_21
        
        # Calculate crossover strength based on separation
        separation_pct = abs((ema_9 - ema_21) / ema_21) * 100
        
        # Bullish crossover: EMA 9 > EMA 21
        if ema_9 > ema_21 and separation_pct > 0.1:  # At least 0.1% separation
            strength = min(separation_pct / 2.0, 1.0)  # Cap at 1.0
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "EMA_CROSSOVER_BULL"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.LONG,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="EMA_CROSSOVER",
                strength=strength,
                features={
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "separation_pct": separation_pct
                },
                reasons=[
                    f"EMA 9 ({ema_9:.2f}) crossed above EMA 21 ({ema_21:.2f})",
                    f"Separation: {separation_pct:.2f}%"
                ],
                timestamp=snapshot.timestamp
            )
        
        # Bearish crossover: EMA 9 < EMA 21
        elif ema_9 < ema_21 and separation_pct > 0.1:
            strength = min(separation_pct / 2.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "EMA_CROSSOVER_BEAR"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.SHORT,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="EMA_CROSSOVER",
                strength=strength,
                features={
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "separation_pct": separation_pct
                },
                reasons=[
                    f"EMA 9 ({ema_9:.2f}) crossed below EMA 21 ({ema_21:.2f})",
                    f"Separation: {separation_pct:.2f}%"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
    
    def _check_price_ema_relationship(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check if price is significantly above/below EMA"""
        if not self._check_required_indicators(snapshot, ['ema_21']):
            return None
        
        price = snapshot.close
        ema_21 = snapshot.ema_21
        
        distance_pct = ((price - ema_21) / ema_21) * 100
        
        # Bullish: Price significantly above EMA
        if distance_pct > 1.5:  # At least 1.5% above
            strength = min(distance_pct / 5.0, 1.0)  # Normalize to 0-1
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "PRICE_ABOVE_EMA"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.LONG,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="PRICE_ABOVE_EMA",
                strength=strength,
                features={
                    "price": price,
                    "ema_21": ema_21,
                    "distance_pct": distance_pct
                },
                reasons=[
                    f"Price ({price:.2f}) is {distance_pct:.2f}% above EMA 21",
                    "Strong bullish momentum"
                ],
                timestamp=snapshot.timestamp
            )
        
        # Bearish: Price significantly below EMA
        elif distance_pct < -1.5:
            strength = min(abs(distance_pct) / 5.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "PRICE_BELOW_EMA"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.SHORT,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="PRICE_BELOW_EMA",
                strength=strength,
                features={
                    "price": price,
                    "ema_21": ema_21,
                    "distance_pct": distance_pct
                },
                reasons=[
                    f"Price ({price:.2f}) is {abs(distance_pct):.2f}% below EMA 21",
                    "Strong bearish momentum"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
    
    def _check_trend_alignment(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check if multiple EMAs are aligned (strong trend)"""
        if not self._check_required_indicators(snapshot, ['ema_9', 'ema_21', 'ema_50']):
            return None
        
        ema_9 = snapshot.ema_9
        ema_21 = snapshot.ema_21
        ema_50 = snapshot.ema_50
        
        # Bullish alignment: EMA 9 > EMA 21 > EMA 50
        if ema_9 > ema_21 > ema_50:
            # Calculate strength based on separation
            sep_1 = ((ema_9 - ema_21) / ema_21) * 100
            sep_2 = ((ema_21 - ema_50) / ema_50) * 100
            strength = min((sep_1 + sep_2) / 3.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "STRONG_UPTREND"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.LONG,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="STRONG_TREND",
                strength=strength,
                features={
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "ema_50": ema_50
                },
                reasons=[
                    "All EMAs aligned bullishly (9 > 21 > 50)",
                    "Strong uptrend confirmed"
                ],
                timestamp=snapshot.timestamp
            )
        
        # Bearish alignment: EMA 9 < EMA 21 < EMA 50
        elif ema_9 < ema_21 < ema_50:
            sep_1 = ((ema_21 - ema_9) / ema_9) * 100
            sep_2 = ((ema_50 - ema_21) / ema_21) * 100
            strength = min((sep_1 + sep_2) / 3.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "STRONG_DOWNTREND"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.SHORT,
                signal_family=SignalFamily.MOMENTUM,
                signal_name="STRONG_TREND",
                strength=strength,
                features={
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "ema_50": ema_50
                },
                reasons=[
                    "All EMAs aligned bearishly (9 < 21 < 50)",
                    "Strong downtrend confirmed"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
