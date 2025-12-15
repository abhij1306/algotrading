"""
Signal History Service - Saves and manages historical trading signals
"""
from typing import Dict, List, Any, Optional
from datetime import datetime, date
from pathlib import Path
import json


class SignalHistoryService:
    """Saves and manages historical trading signals"""
    
    def __init__(self):
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.signals_file = Path(os.path.join(base_dir, 'data', 'signal_history.json'))
        print(f"[SIGNAL HISTORY] Signals file path: {self.signals_file}")
        self.signals = []
        self._load_signals()
    
    def save_signals(self, signals: List[Dict[str, Any]]):
        """Save new signals to history"""
        if not signals:
            return
        
        timestamp = datetime.now().isoformat()
        
        for signal in signals:
            # Add metadata
            signal_record = {
                **signal,
                'scan_timestamp': timestamp,
                'scan_date': date.today().isoformat()
            }
            
            # Check if signal already exists (avoid duplicates)
            if not self._signal_exists(signal_record):
                self.signals.append(signal_record)
        
        self._save_signals()
        print(f"[SIGNAL HISTORY] Saved {len(signals)} signals to history")
    
    def _signal_exists(self, new_signal: Dict) -> bool:
        """Check if signal already exists (by ID)"""
        new_id = new_signal.get('id')
        if not new_id:
            return False
        
        return any(s.get('id') == new_id for s in self.signals)
    
    def get_signals_by_date(self, target_date: str = None) -> List[Dict[str, Any]]:
        """Get signals for a specific date"""
        if not target_date:
            target_date = date.today().isoformat()
        
        return [s for s in self.signals if s.get('scan_date') == target_date]
    
    def get_all_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all historical signals"""
        sorted_signals = sorted(
            self.signals,
            key=lambda x: x.get('scan_timestamp', ''),
            reverse=True
        )
        return sorted_signals[:limit]
    
    def get_signals_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Get signals for a specific symbol"""
        return [s for s in self.signals if s.get('symbol') == symbol]
    
    def clear_old_signals(self, days_to_keep: int = 30):
        """Remove signals older than specified days"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
        
        original_count = len(self.signals)
        self.signals = [
            s for s in self.signals 
            if s.get('scan_timestamp', '') >= cutoff
        ]
        
        removed = original_count - len(self.signals)
        if removed > 0:
            self._save_signals()
            print(f"[SIGNAL HISTORY] Removed {removed} old signals")
    
    def _load_signals(self):
        """Load signals from file"""
        try:
            if self.signals_file.exists():
                with open(self.signals_file, 'r') as f:
                    data = json.load(f)
                    self.signals = data.get('signals', [])
                    print(f"[SIGNAL HISTORY] Loaded {len(self.signals)} historical signals")
        except Exception as e:
            print(f"[SIGNAL HISTORY] Error loading signals: {e}")
            self.signals = []
    
    def _save_signals(self):
        """Save signals to file"""
        try:
            self.signals_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'signals': self.signals,
                'last_updated': datetime.now().isoformat()
            }
            
            with open(self.signals_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[SIGNAL HISTORY] Error saving signals: {e}")


# Global instance
_signal_history = None

def get_signal_history_service() -> SignalHistoryService:
    """Get or create global signal history service"""
    global _signal_history
    
    if _signal_history is None:
        _signal_history = SignalHistoryService()
    
    return _signal_history
