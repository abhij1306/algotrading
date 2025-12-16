"""
Signal Aggregator - Merges RawSignals into CompositeSignals
"""
from typing import List, Dict
from datetime import datetime
from collections import defaultdict
import uuid

from .models import RawSignal, CompositeSignal, Direction, SignalFamily


class SignalAggregator:
    """
    Aggregates multiple RawSignals into CompositeSignals.
    Groups signals by symbol and direction, calculating confluence.
    """
    
    def aggregate(self, raw_signals: List[RawSignal]) -> List[CompositeSignal]:
        """
        Aggregate raw signals into composite signals.
        
        Args:
            raw_signals: List of RawSignal objects
            
        Returns:
            List of CompositeSignal objects
        """
        # Group signals by symbol and direction
        grouped = defaultdict(list)
        for signal in raw_signals:
            key = (signal.symbol, signal.direction, signal.timeframe)
            grouped[key].append(signal)
        
        # Create composite signals
        composite_signals = []
        for (symbol, direction, timeframe), signals in grouped.items():
            if signals:  # Only create composite if there are signals
                composite = self._create_composite(symbol, direction, timeframe, signals)
                composite_signals.append(composite)
        
        # Sort by confluence count (descending) then aggregate strength
        composite_signals.sort(
            key=lambda x: (x.confluence_count, x.aggregate_strength),
            reverse=True
        )
        
        return composite_signals
    
    def _create_composite(
        self,
        symbol: str,
        direction: Direction,
        timeframe: str,
        signals: List[RawSignal]
    ) -> CompositeSignal:
        """
        Create a CompositeSignal from multiple RawSignals.
        
        Args:
            symbol: Stock symbol
            direction: Trade direction
            timeframe: Timeframe
            signals: List of RawSignals to merge
            
        Returns:
            CompositeSignal object
        """
        # Calculate confluence count
        confluence_count = len(signals)
        
        # Calculate aggregate strength (weighted average)
        total_strength = sum(s.strength for s in signals)
        aggregate_strength = total_strength / confluence_count if confluence_count > 0 else 0.0
        
        # Collect signal families and names
        signal_families = list(set(s.signal_family for s in signals))
        signal_names = list(set(s.signal_name for s in signals))
        
        # Merge reasons (deduplicate)
        merged_reasons = []
        seen_reasons = set()
        for signal in signals:
            for reason in signal.reasons:
                if reason not in seen_reasons:
                    merged_reasons.append(reason)
                    seen_reasons.add(reason)
        
        # Merge features
        merged_features = {}
        for signal in signals:
            # Prefix features with signal name to avoid collisions
            for key, value in signal.features.items():
                feature_key = f"{signal.signal_name}_{key}"
                merged_features[feature_key] = value
        
        # Get timestamps
        timestamps = [s.timestamp for s in signals]
        first_signal_time = min(timestamps)
        last_signal_time = max(timestamps)
        
        # Generate composite ID
        composite_id = f"COMP_{symbol}_{direction.value}_{uuid.uuid4().hex[:8]}"
        
        return CompositeSignal(
            composite_id=composite_id,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            confluence_count=confluence_count,
            aggregate_strength=aggregate_strength,
            signal_families=signal_families,
            signal_names=signal_names,
            merged_reasons=merged_reasons,
            merged_features=merged_features,
            first_signal_time=first_signal_time,
            last_signal_time=last_signal_time
        )
