"""
Index alignment signal generator
"""
from typing import List
from .base import SignalGenerator
from ..models import MarketSnapshot, RawSignal, Direction, SignalFamily


class IndexAlignmentGenerator(SignalGenerator):
    """
    Generates signals based on stock's relationship with Nifty index.
    
    Signals:
    - INDEX_ALIGNED: Stock moving with Nifty
    - OUTPERFORMING: Stock outperforming index
    - UNDERPERFORMING: Stock underperforming index
    """
    
    @property
    def signal_family(self) -> str:
        return SignalFamily.INDEX_ALIGNMENT.value
    
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """Generate index alignment signals"""
        signals = []
        
        aligned_signal = self._check_index_alignment(snapshot)
        if aligned_signal:
            signals.append(aligned_signal)
        
        performance_signal = self._check_relative_performance(snapshot)
        if performance_signal:
            signals.append(performance_signal)
        
        return signals
    
    def _check_index_alignment(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check if stock is moving with Nifty"""
        if not snapshot.nifty_change_pct or not snapshot.correlation_with_nifty:
            return None
        
        correlation = snapshot.correlation_with_nifty
        nifty_change = snapshot.nifty_change_pct
        stock_change = snapshot.change_pct
        
        # High correlation and both moving in same direction
        if correlation > 0.7 and abs(correlation) > 0.7:
            # Both positive
            if nifty_change > 0.5 and stock_change > 0.5:
                strength = min(correlation, 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "INDEX_ALIGNED_BULL"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.LONG,
                    signal_family=SignalFamily.INDEX_ALIGNMENT,
                    signal_name="INDEX_ALIGNED",
                    strength=strength,
                    features={
                        "correlation": correlation,
                        "nifty_change_pct": nifty_change,
                        "stock_change_pct": stock_change
                    },
                    reasons=[
                        f"Moving with Nifty (correlation: {correlation:.2f})",
                        f"Nifty: {nifty_change:+.2f}%, Stock: {stock_change:+.2f}%",
                        "Strong market alignment"
                    ],
                    timestamp=snapshot.timestamp
                )
            
            # Both negative
            elif nifty_change < -0.5 and stock_change < -0.5:
                strength = min(abs(correlation), 1.0)
                
                return RawSignal(
                    signal_id=self._generate_signal_id(snapshot, "INDEX_ALIGNED_BEAR"),
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    direction=Direction.SHORT,
                    signal_family=SignalFamily.INDEX_ALIGNMENT,
                    signal_name="INDEX_ALIGNED",
                    strength=strength,
                    features={
                        "correlation": correlation,
                        "nifty_change_pct": nifty_change,
                        "stock_change_pct": stock_change
                    },
                    reasons=[
                        f"Moving with Nifty (correlation: {correlation:.2f})",
                        f"Nifty: {nifty_change:+.2f}%, Stock: {stock_change:+.2f}%",
                        "Following market weakness"
                    ],
                    timestamp=snapshot.timestamp
                )
        
        return None
    
    def _check_relative_performance(self, snapshot: MarketSnapshot) -> RawSignal | None:
        """Check if stock is outperforming/underperforming index"""
        if not snapshot.relative_strength or not snapshot.nifty_change_pct:
            return None
        
        relative_strength = snapshot.relative_strength
        
        # Outperforming: Stock doing better than Nifty
        if relative_strength > 1.5:  # 50% better than index
            strength = min(relative_strength / 3.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "OUTPERFORMING"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.LONG,
                signal_family=SignalFamily.INDEX_ALIGNMENT,
                signal_name="OUTPERFORMING",
                strength=strength,
                features={
                    "relative_strength": relative_strength,
                    "nifty_change_pct": snapshot.nifty_change_pct,
                    "stock_change_pct": snapshot.change_pct
                },
                reasons=[
                    f"Outperforming index by {(relative_strength - 1) * 100:.0f}%",
                    f"Stock: {snapshot.change_pct:+.2f}% vs Nifty: {snapshot.nifty_change_pct:+.2f}%",
                    "Strong relative strength"
                ],
                timestamp=snapshot.timestamp
            )
        
        # Underperforming: Stock doing worse than Nifty
        elif relative_strength < -1.5:
            strength = min(abs(relative_strength) / 3.0, 1.0)
            
            return RawSignal(
                signal_id=self._generate_signal_id(snapshot, "UNDERPERFORMING"),
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                direction=Direction.SHORT,
                signal_family=SignalFamily.INDEX_ALIGNMENT,
                signal_name="UNDERPERFORMING",
                strength=strength,
                features={
                    "relative_strength": relative_strength,
                    "nifty_change_pct": snapshot.nifty_change_pct,
                    "stock_change_pct": snapshot.change_pct
                },
                reasons=[
                    f"Underperforming index by {abs(relative_strength + 1) * 100:.0f}%",
                    f"Stock: {snapshot.change_pct:+.2f}% vs Nifty: {snapshot.nifty_change_pct:+.2f}%",
                    "Weak relative strength"
                ],
                timestamp=snapshot.timestamp
            )
        
        return None
