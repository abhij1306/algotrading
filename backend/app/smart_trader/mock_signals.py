"""
Mock signal generator for Smart Trader testing
"""
from typing import List, Dict, Any
import random
from datetime import datetime

def generate_mock_signals(count: int = 3) -> List[Dict[str, Any]]:
    """Generate mock trading signals for UI testing"""
    
    symbols = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
    directions = ['LONG', 'SHORT']
    
    signals = []
    
    for i in range(min(count, len(symbols))):
        symbol = symbols[i]
        direction = random.choice(directions)
        entry = round(random.uniform(1000, 3000), 2)
        
        if direction == 'LONG':
            stop_loss = round(entry * 0.98, 2)
            target = round(entry * 1.03, 2)
        else:
            stop_loss = round(entry * 1.02, 2)
            target = round(entry * 0.97, 2)
        
        signal = {
            'symbol': symbol,
            'direction': direction,
            'entry': entry,
            'stop_loss': stop_loss,
            'target': target,
            'quantity': random.randint(10, 50),
            'momentum_score': round(random.uniform(50, 80), 2),
            'confidence': random.choice(['HIGH', 'MEDIUM']),
            'reasons': [
                f"Price {'above' if direction == 'LONG' else 'below'} EMA20",
                f"Volume spike {random.randint(150, 200)}%",
                f"ATR expansion {random.uniform(1.5, 2.5):.1f}%"
            ],
            'timestamp': datetime.now().isoformat(),
            'trade_type': 'STOCK_SPOT'
        }
        
        signals.append(signal)
    
    return signals
