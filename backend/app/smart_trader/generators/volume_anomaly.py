"""
Volume-based signal generator
"""
from typing import List
from .base import SignalGenerator
from ..models import MarketSnapshot, RawSignal, Direction, SignalFamily


class VolumeAnomalyGenerator(SignalGenerator):
    """
    Generates signals based on volume anomalies and volume-price confirmation.
    
    Signals:
    - VOLUME_SPIKE: Unusual volume increase
    - VOLUME_BREAKOUT: Volume spike with price breakout
    """
    
    @property
    def signal_family(self) -> str:
        return SignalFamily.VOLUME.value
    
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """Generate volume-based signals"""
        signals = []
        
        volume_spike = self._check_volume_spike(snapshot)
        if volume_spike:
            signals.append(volume_spike)
        
        volume_breakout = self._check_volume_breakout(snapshot)
        if volume_breakout:
            signals.append(volume_breakout)
        
        return signals
    
    def _check_volume_spike(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for volume spike vs average"""
        if not snapshot.avg_volume_20 or not snapshot.volume_ratio:
            return None
        
        volume_ratio = snapshot.volume_ratio
        
        # Volume spike: 2x or more of average
        if volume_ratio >= 2.0:
            strength = min(volume_ratio / 5.0, 1.0)  # Normalize to 0-1
            
            # Determine direction based on price movement
            direction = Direction.LONG if snapshot.change_pct > 0 else Direction.SHORT
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "VOLUME_SPIKE"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=direction,
                signal_family=SignalFamily.VOLUME,
                signal_name="VOLUME_SPIKE",
                strength=strength,
                features={
                    "volume": snapshot.volume,
                    "avg_volume": snapshot.avg_volume_20,
                    "volume_ratio": volume_ratio,
                    "price_change_pct": snapshot.change_pct
                },
                reasons=[
                    f"Volume spike: {volume_ratio:.1f}x average",
                    f"Current volume: {snapshot.volume:,}",
                    f"Price change: {snapshot.change_pct:+.2f}%"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
    
    def _check_volume_breakout(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check for volume spike with significant price movement"""
        if not snapshot.volume_ratio or not snapshot.atr:
            return None
        
        volume_ratio = snapshot.volume_ratio
        price_range = snapshot.range_pct
        
        # Volume breakout: High volume + significant range
        if volume_ratio >= 1.5 and price_range >= 2.0:
            strength = min((volume_ratio + price_range / 2.0) / 5.0, 1.0)
            
            # Bullish breakout
            if snapshot.change_pct > 1.0 and snapshot.close > snapshot.open:
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "VOLUME_BREAKOUT_BULL"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.LONG,
                    signal_family=SignalFamily.VOLUME,
                    signal_name="VOLUME_BREAKOUT",
                    strength=strength,
                    features={
                        "volume_ratio": volume_ratio,
                        "price_range_pct": price_range,
                        "change_pct": snapshot.change_pct
                    },
                    reasons=[
                        f"Volume breakout: {volume_ratio:.1f}x with bullish candle",
                        f"Price range: {price_range:.2f}%",
                        f"Strong buying pressure"
                    ],
                    timestamp=snapshot.timestamp
                )
            
            # Bearish breakout
            elif snapshot.change_pct < -1.0 and snapshot.close < snapshot.open:
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "VOLUME_BREAKOUT_BEAR"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.SHORT,
                    signal_family=SignalFamily.VOLUME,
                    signal_name="VOLUME_BREAKOUT",
                    strength=strength,
                    features={
                        "volume_ratio": volume_ratio,
                        "price_range_pct": price_range,
                        "change_pct": snapshot.change_pct
                    },
                    reasons=[
                        f"Volume breakout: {volume_ratio:.1f}x with bearish candle",
                        f"Price range: {price_range:.2f}%",
                        f"Strong selling pressure"
                    ],
                    timestamp=snapshot.timestamp
                )
        
        return None
