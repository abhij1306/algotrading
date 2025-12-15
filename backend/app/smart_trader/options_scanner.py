"""
Index Options Scanner Agent - Scans NIFTY/BANKNIFTY options for momentum signals
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from ..data_fetcher import fetch_historical_data
from ..indicators import ema, rsi
from .utils import calculate_target, get_lot_size


class OptionsScannerAgent:
    """Scans Index Options (NIFTY, BANKNIFTY) for high-probability trades"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.indices = config.get('universe', {}).get('indices', ['NIFTY', 'BANKNIFTY'])
        self.min_momentum_score = config.get('scanner', {}).get('min_momentum_score', 60)
    
    def scan(self) -> List[Dict[str, Any]]:
        """
        Scan index options and generate signals
        
        Returns:
            List of options signal dictionaries
        """
        signals = []
        
        for index in self.indices:
            try:
                # Analyze underlying index first
                index_signal = self._analyze_index(index)
                
                if index_signal:
                    # Generate option signal based on index direction
                    option_signal = self._generate_option_signal(index, index_signal)
                    
                    if option_signal and option_signal['momentum_score'] >= self.min_momentum_score:
                        signals.append(option_signal)
                        
            except Exception as e:
                print(f"Error scanning {index} options: {e}")
                continue
        
        # Sort by momentum score
        signals.sort(key=lambda x: x['momentum_score'], reverse=True)
        
        return signals
    
    def _analyze_index(self, index: str) -> Optional[Dict[str, Any]]:
        """
        Analyze underlying index for trend and momentum
        
        Args:
            index: Index name (NIFTY or BANKNIFTY)
            
        Returns:
            Index analysis result
        """
        # Fetch historical data from database
        # For indices, try using standard symbol format
        symbol = f"NIFTY 50" if index == "NIFTY" else "NIFTY BANK"
        
        df = fetch_historical_data(
            symbol=symbol,
            days=30  # Get 30 days of data
        )
        
        if df is None or df.empty or len(df) < 20:
            return None
        
        # Calculate indicators
        df['ema20'] = ema(df['close'], span=20)
        df['ema50'] = ema(df['close'], span=50)
        df['rsi'] = rsi(df['close'], n=14)
        
        # ATR
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            )
        )
        df['atr'] = df['tr'].rolling(window=14).mean()
        df['atr_pct'] = (df['atr'] / df['close']) * 100
        
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        
        # Determine trend
        direction = None
        score = 0
        reasons = []
        
        # Trend check
        if latest['close'] > latest['ema20'] and latest['ema20'] > latest['ema50']:
            direction = 'BULLISH'
            reasons.append("Index in uptrend")
            score += 30
        elif latest['close'] < latest['ema20'] and latest['ema20'] < latest['ema50']:
            direction = 'BEARISH'
            reasons.append("Index in downtrend")
            score += 30
        
        if not direction:
            return None
        
        # Range expansion
        current_range = latest['high'] - latest['low']
        avg_range = (df['high'] - df['low']).tail(20).mean()
        
        if current_range > avg_range * 1.2:
            reasons.append("Range expanding")
            score += 20
        
        # Momentum
        price_change_pct = ((latest['close'] - prev['close']) / prev['close']) * 100
        if abs(price_change_pct) > 0.3:
            reasons.append(f"Strong momentum ({price_change_pct:+.1f}%)")
            score += 15
        
        # Volatility
        if latest['atr_pct'] > 0.5:
            reasons.append(f"Good volatility (ATR {latest['atr_pct']:.1f}%)")
            score += 15
        
        # RSI confirmation
        if direction == 'BULLISH' and 40 <= latest['rsi'] <= 70:
            reasons.append(f"RSI {latest['rsi']:.0f} (bullish)")
            score += 10
        elif direction == 'BEARISH' and 30 <= latest['rsi'] <= 60:
            reasons.append(f"RSI {latest['rsi']:.0f} (bearish)")
            score += 10
        
        return {
            'index': index,
            'direction': direction,
            'score': score,
            'reasons': reasons,
            'spot_price': latest['close'],
            'atr': latest['atr'],
            'rsi': latest['rsi'],
            'volatility_state': 'EXPANDING' if current_range > avg_range * 1.2 else 'NORMAL'
        }
    
    def _generate_option_signal(self, index: str, index_signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generate option trading signal based on index analysis
        
        Args:
            index: Index name
            index_signal: Analysis result from index
            
        Returns:
            Option signal dictionary
        """
        spot_price = index_signal['spot_price']
        direction = 'LONG' if index_signal['direction'] == 'BULLISH' else 'SHORT'
        
        # Determine ATM strike (round to nearest strike)
        strike_interval = 50 if index == 'NIFTY' else 100
        atm_strike = round(spot_price / strike_interval) * strike_interval
        
        # Select option type based on direction
        if direction == 'LONG':
            option_type = 'CE'  # Call option
            strike = atm_strike  # ATM call
        else:
            option_type = 'PE'  # Put option
            strike = atm_strike  # ATM put
        
        # Generate option symbol (simplified - in production, fetch actual tradable symbols)
        # Format: NIFTY24DEC22500CE
        expiry = self._get_nearest_weekly_expiry()
        symbol = f"{index}{expiry}{strike}{option_type}"
        
        # Estimate option premium (simplified - in production, fetch live data)
        # For demo, use a rough estimation
        premium = self._estimate_option_premium(index, spot_price, strike, option_type, index_signal['atr'])
        
        # Calculate position details
        lot_size = get_lot_size(index)
        
        # Stop loss and target (for options, use percentage-based on premium)
        stop_loss_pct = 30  # 30% loss
        target_pct = 50  # 50% profit (1.67:1 R:R)
        
        stop_loss = premium * (1 - stop_loss_pct / 100)
        target = premium * (1 + target_pct / 100)
        
        # Score adjustment for options
        score = index_signal['score']
        
        # Add option-specific factors
        if index_signal['volatility_state'] == 'EXPANDING':
            score += 10
        
        # Confidence
        if score >= 80:
            confidence = "HIGH"
        elif score >= 65:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        
        return {
            'instrument_type': 'OPTION',
            'index': index,
            'symbol': symbol,
            'direction': direction,
            'option_type': option_type,
            'strike': strike,
            'spot_price': round(spot_price, 2),
            'momentum_score': min(score, 100),
            'confidence': confidence,
            'volatility_state': index_signal['volatility_state'],
            'reasons': index_signal['reasons'],
            'premium': round(premium, 2),
            'entry_price': round(premium, 2),
            'stop_loss': round(stop_loss, 2),
            'target': round(target, 2),
            'lot_size': lot_size,
            'quantity': lot_size,  # 1 lot
            'timestamp': datetime.now().isoformat()
        }
    
    def _get_nearest_weekly_expiry(self) -> str:
        """
        Get nearest weekly expiry in DDMMM format (e.g., 19DEC)
        Simplified - in production, use actual expiry calendar
        """
        # Find next Thursday (weekly expiry day)
        today = datetime.now()
        days_ahead = 3 - today.weekday()  # Thursday is 3
        
        if days_ahead <= 0:  # If today is Thursday or later, get next Thursday
            days_ahead += 7
        
        next_thursday = today + timedelta(days=days_ahead)
        
        return next_thursday.strftime("%d%b").upper()
    
    def _estimate_option_premium(self, index: str, spot: float, strike: float, option_type: str, atr: float) -> float:
        """
        Rough estimation of option premium
        In production, fetch live premium from broker API
        
        Args:
            index: Index name
            spot: Spot price
            strike: Strike price
            option_type: CE or PE
            atr: ATR value
            
        Returns:
            Estimated premium
        """
        # Simple intrinsic + time value estimation
        if option_type == 'CE':
            intrinsic = max(spot - strike, 0)
        else:  # PE
            intrinsic = max(strike - spot, 0)
        
        # Time value (rough approximation using ATR)
        time_value = atr * 0.5  # Simplified estimation
        
        premium = intrinsic + time_value
        
        # Ensure minimum premium
        return max(premium, 10)  # Minimum ₹10
