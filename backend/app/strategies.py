"""
Trading Strategies with configurable weightages
User can select different strategies for different market conditions
"""
from typing import Dict, List
from dataclasses import dataclass

@dataclass
class StrategyWeights:
    """Weightage configuration for a strategy"""
    volume_surge: int
    breakout: int
    trend: int
    volatility: int
    momentum: int = 0
    price_deviation: int = 0
    
    def total(self) -> int:
        return (self.volume_surge + self.breakout + self.trend + 
                self.volatility + self.momentum + self.price_deviation)


# ==================== STRATEGY DEFINITIONS ====================

STRATEGIES = {
    'momentum': {
        'name': 'Momentum Stocks',
        'description': 'Strong upward momentum with volume confirmation',
        'icon': '🚀',
        'weights': StrategyWeights(
            volume_surge=25,      # High volume important
            breakout=30,          # Breaking resistance
            trend=20,             # Strong trend
            volatility=10,        # Lower volatility preference
            momentum=15,          # Price momentum key
            price_deviation=0
        ),
        'filters': {
            'min_volume_percentile': 70,
            'min_rsi': 55,
            'max_rsi': 75,
            'trend_required': True
        }
    },
    
    'price_movers': {
        'name': 'Price Movers',
        'description': 'Stocks with significant price deviation from average',
        'icon': '📊',
        'weights': StrategyWeights(
            volume_surge=20,
            breakout=15,
            trend=10,
            volatility=15,
            momentum=10,
            price_deviation=30    # Key factor
        ),
        'filters': {
            'min_price_change': 2.0,  # 2% minimum move
            'min_volume_percentile': 50
        }
    },
    
    'breakout': {
        'name': 'Breakout Stocks',
        'description': 'Breaking 20-day highs with volume',
        'icon': '💥',
        'weights': StrategyWeights(
            volume_surge=30,
            breakout=40,          # Primary focus
            trend=15,
            volatility=10,
            momentum=5,
            price_deviation=0
        ),
        'filters': {
            'breakout_required': True,
            'min_volume_percentile': 75
        }
    },
    
    'swing_trade': {
        'name': 'Swing Trading',
        'description': 'Multi-day position trades',
        'icon': '📈',
        'weights': StrategyWeights(
            volume_surge=20,
            breakout=25,
            trend=30,             # Trend most important
            volatility=15,
            momentum=10,
            price_deviation=0
        ),
        'filters': {
            'min_atr_pct': 1.5,
            'max_atr_pct': 4.0,
            'trend_required': True
        }
    },
    
    'intraday': {
        'name': 'Intraday Trading',
        'description': 'Same-day trades with tight stops',
        'icon': '⚡',
        'weights': StrategyWeights(
            volume_surge=30,
            breakout=25,
            trend=15,
            volatility=30,        # Tight volatility important
            momentum=0,
            price_deviation=0
        ),
        'filters': {
            'min_atr_pct': 1.0,
            'max_atr_pct': 3.0,
            'min_volume_percentile': 60
        }
    },
    
    'value_momentum': {
        'name': 'Value + Momentum',
        'description': 'Undervalued stocks gaining momentum',
        'icon': '💎',
        'weights': StrategyWeights(
            volume_surge=20,
            breakout=20,
            trend=25,
            volatility=15,
            momentum=20,
            price_deviation=0
        ),
        'filters': {
            'max_pe_ratio': 25,   # Value filter
            'min_volume_percentile': 60
        }
    },
    
    'scalping': {
        'name': 'Scalping',
        'description': 'Quick in-and-out trades',
        'icon': '⚡️',
        'weights': StrategyWeights(
            volume_surge=35,      # Very high volume needed
            breakout=20,
            trend=10,
            volatility=25,        # Tight range
            momentum=10,
            price_deviation=0
        ),
        'filters': {
            'min_volume_percentile': 80,
            'max_atr_pct': 2.0,
            'min_liquidity': 1000000  # High liquidity
        }
    },
    
    'gap_trading': {
        'name': 'Gap Trading',
        'description': 'Stocks with opening gaps',
        'icon': '🎯',
        'weights': StrategyWeights(
            volume_surge=25,
            breakout=15,
            trend=15,
            volatility=15,
            momentum=10,
            price_deviation=20    # Gap detection
        ),
        'filters': {
            'min_gap_pct': 1.5,   # 1.5% gap minimum
            'min_volume_percentile': 65
        }
    }
}


def get_strategy(strategy_name: str) -> Dict:
    """Get strategy configuration"""
    return STRATEGIES.get(strategy_name, STRATEGIES['intraday'])


def get_all_strategies() -> List[Dict]:
    """Get all available strategies"""
    return [
        {
            'id': key,
            'name': config['name'],
            'description': config['description'],
            'icon': config['icon']
        }
        for key, config in STRATEGIES.items()
    ]


def calculate_score_with_strategy(features: Dict, strategy_name: str) -> float:
    """
    Calculate score based on selected strategy
    
    Args:
        features: Stock features dict
        strategy_name: Strategy to use
        
    Returns:
        Score (0-100)
    """
    strategy = get_strategy(strategy_name)
    weights = strategy['weights']
    filters = strategy.get('filters', {})
    
    # Apply filters first
    if not passes_filters(features, filters):
        return 0.0
    
    score = 0.0
    
    # Volume surge score
    vol_pct = features.get('vol_percentile', 0)
    if vol_pct > 80:
        vol_score = weights.volume_surge
    elif vol_pct > 60:
        vol_score = weights.volume_surge * 0.7
    elif vol_pct > 40:
        vol_score = weights.volume_surge * 0.4
    else:
        vol_score = 0
    score += vol_score
    
    # Breakout score
    if features.get('is_20d_breakout', False):
        score += weights.breakout
    elif features.get('close', 0) > features.get('20d_high', 0) * 0.98:
        score += weights.breakout * 0.5
    
    # Trend score
    if features.get('price_above_ema50', False) and features.get('ema20_above_50', False):
        score += weights.trend
    elif features.get('price_above_ema50', False):
        score += weights.trend * 0.5
    
    # Volatility score
    atr_pct = features.get('atr_pct', 0)
    if 1.5 <= atr_pct <= 3.0:
        score += weights.volatility
    elif 1.0 <= atr_pct <= 4.0:
        score += weights.volatility * 0.6
    
    # Momentum score (RSI based)
    if weights.momentum > 0:
        rsi = features.get('rsi', 50)
        if 55 <= rsi <= 70:
            score += weights.momentum
        elif 50 <= rsi <= 75:
            score += weights.momentum * 0.5
    
    # Price deviation score
    if weights.price_deviation > 0:
        z_close = abs(features.get('z_close', 0))
        if z_close > 1.5:
            score += weights.price_deviation
        elif z_close > 1.0:
            score += weights.price_deviation * 0.6
    
    # Normalize to 0-100
    max_possible = weights.total()
    normalized_score = (score / max_possible) * 100 if max_possible > 0 else 0
    
    return min(100, max(0, normalized_score))


def passes_filters(features: Dict, filters: Dict) -> bool:
    """Check if stock passes strategy filters"""
    
    # Volume filter
    if 'min_volume_percentile' in filters:
        if features.get('vol_percentile', 0) < filters['min_volume_percentile']:
            return False
    
    # RSI filters
    if 'min_rsi' in filters:
        if features.get('rsi', 0) < filters['min_rsi']:
            return False
    if 'max_rsi' in filters:
        if features.get('rsi', 100) > filters['max_rsi']:
            return False
    
    # Trend required
    if filters.get('trend_required', False):
        if not (features.get('price_above_ema50', False) and features.get('ema20_above_50', False)):
            return False
    
    # Breakout required
    if filters.get('breakout_required', False):
        if not features.get('is_20d_breakout', False):
            return False
    
    # ATR filters
    if 'min_atr_pct' in filters:
        if features.get('atr_pct', 0) < filters['min_atr_pct']:
            return False
    if 'max_atr_pct' in filters:
        if features.get('atr_pct', 100) > filters['max_atr_pct']:
            return False
    
    # Price change filter
    if 'min_price_change' in filters:
        # Calculate from z_close or other metric
        if abs(features.get('z_close', 0)) < 0.5:
            return False
    
    return True
