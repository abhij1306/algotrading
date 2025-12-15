"""
Decision Agent - Merges, ranks, and presents trade candidates to user
"""
from typing import List, Dict, Any
from datetime import datetime


class DecisionAgent:
    """Merges signals from scanners, ranks candidates, removes duplicates"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    def process_signals(
        self, 
        stock_signals: List[Dict[str, Any]], 
        options_signals: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Merge and rank all signals from both scanners
        
        Args:
            stock_signals: List of stock signals
            options_signals: List of option signals
            
        Returns:
            Ranked list of all trade candidates
        """
        # Combine all signals
        all_signals = stock_signals + options_signals
        
        # Remove correlated signals
        filtered_signals = self._remove_correlated_signals(all_signals)
        
        # Rank by composite score
        ranked_signals = self._rank_signals(filtered_signals)
        
        return ranked_signals
    
    def _remove_correlated_signals(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Remove highly correlated signals
        E.g., if both NIFTY stock and NIFTY option signal, keep the higher score
        """
        filtered = []
        seen_indices = {}
        
        for signal in signals:
            # For options, check if we already have a signal for that index
            if signal['instrument_type'] == 'OPTION':
                index = signal['index']
                
                if index in seen_indices:
                    # Keep the one with higher score
                    existing = seen_indices[index]
                    if signal['momentum_score'] > existing['momentum_score']:
                        # Replace with higher score
                        filtered.remove(existing)
                        filtered.append(signal)
                        seen_indices[index] = signal
                else:
                    filtered.append(signal)
                    seen_indices[index] = signal
            else:
                # For stocks, just add (no correlation check for now)
                filtered.append(signal)
        
        return filtered
    
    def _rank_signals(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Rank signals by composite score
        
        Ranking formula:
        - Momentum Score: 60%
        - Confidence Weight: 20%
        - Risk/Reward: 20%
        """
        for signal in signals:
            # Confidence weights
            confidence_weight = {
                'HIGH': 20,
                'MEDIUM': 15,
                'LOW': 10
            }.get(signal.get('confidence', 'MEDIUM'), 15)
            
            # Risk/Reward score (normalize to 0-20)
            entry = signal.get('entry_price', 0)
            stop_loss = signal.get('stop_loss', 0)
            target = signal.get('target', 0)
            
            if entry and stop_loss and target:
                risk = abs(entry - stop_loss)
                reward = abs(target - entry)
                rr_ratio = reward / risk if risk > 0 else 0
                rr_score = min(rr_ratio * 10, 20)  # Cap at 20, 2:1 RR = 20 points
            else:
                rr_score = 0
            
            # Calculate final composite score
            momentum_score = signal.get('momentum_score', 0)
            composite_score = (momentum_score * 0.6) + confidence_weight + rr_score
            
            signal['composite_score'] = round(composite_score, 2)
            signal['rank'] = 0  # Will be set after sorting
        
        # Sort by composite score (highest first)
        signals.sort(key=lambda x: x['composite_score'], reverse=True)
        
        # Assign ranks
        for idx, signal in enumerate(signals, 1):
            signal['rank'] = idx
        
        return signals
    
    def get_top_candidates(self, signals: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
        """Get top N trade candidates"""
        return signals[:limit]
    
    def format_for_display(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Format signal for frontend display"""
        return {
            'id': f"{signal['symbol']}_{signal['timestamp']}",
            'rank': signal.get('rank', 0),
            'symbol': signal['symbol'],
            'instrument_type': signal['instrument_type'],
            'direction': signal['direction'],
            'momentum_score': signal['momentum_score'],
            'composite_score': signal.get('composite_score', 0),
            'confidence': signal['confidence'],
            'entry_price': signal['entry_price'],
            'stop_loss': signal['stop_loss'],
            'target': signal['target'],
            'reasons': signal['reasons'],
            'risk_reward_ratio': round(
                abs(signal['target'] - signal['entry_price']) / abs(signal['entry_price'] - signal['stop_loss']), 
                2
            ) if signal['entry_price'] != signal['stop_loss'] else 0,
            'timestamp': signal['timestamp'],
            # Additional fields for options
            **({
                'index': signal.get('index'),
                'strike': signal.get('strike'),
                'option_type': signal.get('option_type'),
                'lot_size': signal.get('lot_size'),
                'premium': signal.get('premium')
            } if signal['instrument_type'] == 'OPTION' else {})
        }
