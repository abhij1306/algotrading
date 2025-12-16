"""
Base class for all signal generators
"""
from abc import ABC, abstractmethod
from typing import List
from ..models import MarketSnapshot, RawSignal


class SignalGenerator(ABC):
    """
    Abstract base class for deterministic signal generators.
    
    Core principles:
    - Never depends on LLM
    - Never blocks on missing indicators
    - Returns empty list if conditions not met
    - Generates RawSignals with clear reasons
    """
    
    def __init__(self, config: dict = None):
        """
        Initialize generator with optional configuration.
        
        Args:
            config: Generator-specific configuration
        """
        self.config = config or {}
    
    @abstractmethod
    def generate(self, snapshot: MarketSnapshot) -> List[RawSignal]:
        """
        Generate signals from market snapshot.
        
        Args:
            snapshot: MarketSnapshot containing all market data
            
        Returns:
            List of RawSignal objects (empty if no signals)
            
        Note:
            - Must handle missing indicators gracefully
            - Must not raise exceptions for missing data
            - Must provide clear reasons for each signal
        """
        pass
    
    @property
    @abstractmethod
    def signal_family(self) -> str:
        """Return the signal family this generator belongs to"""
        pass
    
    def _check_required_indicators(self, snapshot: MarketSnapshot, indicators: List[str]) -> bool:
        """
        Check if required indicators are available in snapshot.
        
        Args:
            snapshot: MarketSnapshot to check
            indicators: List of required indicator names
            
        Returns:
            True if all indicators are available, False otherwise
        """
        return all(snapshot.has_indicator(ind) for ind in indicators)
    
    def _generate_signal_id(self, snapshot: MarketSnapshot, signal_name: str) -> str:
        """
        Generate unique signal ID.
        
        Args:
            snapshot: MarketSnapshot
            signal_name: Name of the signal
            
        Returns:
            Unique signal ID string
        """
        timestamp_str = snapshot.timestamp.strftime("%Y%m%d%H%M%S")
        return f"{snapshot.symbol}_{signal_name}_{timestamp_str}"
