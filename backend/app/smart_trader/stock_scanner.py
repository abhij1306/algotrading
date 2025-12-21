"""
Stock Scanner Agent - Scans NSE F&O stocks for momentum signals
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from ..data_fetcher import fetch_historical_data
from ..indicators import ema, rsi
from .utils import calculate_atr_stop_loss, calculate_target, get_nse_fo_universe
from .config_utils import get_config_value


class StockScannerAgent:
    """Scans NSE F&O stocks for intraday momentum signals"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.universe = self._get_universe()
        # Initial load (fallback to config dict), but dynamic refresh happens in scan
        self.min_momentum_score = get_config_value("SCANNER_MIN_SCORE", config.get('scanner', {}).get('min_momentum_score', 25))
        self.min_volume_percentile = get_config_value("SCANNER_MIN_VOL_PCT", config.get('scanner', {}).get('min_volume_percentile', 40))
        self.min_atr_pct = get_config_value("SCANNER_MIN_ATR_PCT", config.get('scanner', {}).get('min_atr_pct', 0.8))
    
    def _get_universe(self) -> List[str]:
        """Get NSE F&O universe"""
        universe_type = self.config.get('universe', {}).get('stocks', 'nse_fo')
        
        if universe_type == 'nse_fo':
            return get_nse_fo_universe()
        
        return []
    
    def scan(self, use_live_prices: bool = True) -> List[Dict[str, Any]]:
        """
        Scan stocks for trading signals in PARALLEL
        """
        # Refresh Configs Dynamic logic
        self.min_momentum_score = get_config_value("SCANNER_MIN_SCORE", self.min_momentum_score)
        self.min_volume_percentile = get_config_value("SCANNER_MIN_VOL_PCT", self.min_volume_percentile)
        
        import concurrent.futures
        signals = []
        
        # Fetch live prices for all stocks if enabled
        live_prices = {}
        if use_live_prices:
            try:
                from .live_price_service import get_live_price_service
                price_service = get_live_price_service()
                live_prices = price_service.get_live_prices(self.universe)
                print(f"[STOCK SCANNER] Fetched live prices for {len(live_prices)} stocks")
            except Exception as e:
                print(f"[STOCK SCANNER] Could not fetch live prices: {e}. Using database data.")
                live_prices = {}
        
        # Scan each stock in parallel
        print(f"[STOCK SCANNER] Scanning {len(self.universe)} stocks in PARALLEL...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Prepare arguments for map
            # We need to pass symbol and its corresponding live price (if any)
            # Since map takes one iterable, we'll wrap the logic in a lambda or helper
            
            future_to_symbol = {
                executor.submit(self._analyze_stock_safe, symbol, live_prices.get(symbol)): symbol 
                for symbol in self.universe
            }
            
            for future in concurrent.futures.as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    signal = future.result()
                    if signal:
                        print(f"[STOCK SCANNER] {symbol}: Signal generated (score={signal['momentum_score']})")
                        if signal['momentum_score'] >= self.min_momentum_score:
                            signals.append(signal)
                        else:
                            print(f"[STOCK SCANNER] {symbol}: Score {signal['momentum_score']} below threshold")
                except Exception as e:
                    print(f"[STOCK SCANNER] Error scanning {symbol}: {e}")

        # Sort by momentum score (highest first)
        signals.sort(key=lambda x: x['momentum_score'], reverse=True)
        
        print(f"[STOCK SCANNER] Total signals: {len(signals)}")
        return signals

    def _analyze_stock_safe(self, symbol, live_price_data):
        """Wrapper for _analyze_stock to handle exceptions safely in threads"""
        try:
            return self._analyze_stock(symbol, live_price_data)
        except Exception as e:
            print(f"Error in thread for {symbol}: {e}")
            return None
    
    def _analyze_stock(self, symbol: str, live_price_data: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Analyze individual stock and generate signal if criteria met
        
        Args:
            symbol: Stock symbol
            live_price_data: Optional live price data {ltp, change_pct, volume, high, low}
            
        Returns:
            Signal dictionary or None
        """
        # Fetch historical data from database (daily data)
        print(f"[SCANNER] Analyzing {symbol}...")
        df = fetch_historical_data(
            symbol=symbol,
            days=30  # Get 30 days of daily data
        )
        
        if df is None:
            print(f"[SCANNER] {symbol}: fetch_historical_data returned None")
            return None
            
        if df.empty:
            print(f"[SCANNER] {symbol}: DataFrame is empty")
            return None
            
        print(f"[SCANNER] {symbol}: Got {len(df)} rows of data")
        
        if len(df) < 20:
            print(f"[SCANNER] {symbol}: Insufficient data ({len(df)} < 20)")
            return None
        
        # Calculate indicators using historical data
        df = self._calculate_indicators(df)
        
        # Get latest candle and previous
        latest = df.iloc[-1].copy()
        prev = df.iloc[-2] if len(df) > 1 else latest
        
        # If live prices available, update latest candle with live data
        if live_price_data and live_price_data.get('ltp'):
            print(f"[SCANNER] {symbol}: Using live price ₹{live_price_data['ltp']:.2f} (DB: ₹{latest['close']:.2f})")
            latest['close'] = live_price_data['ltp']
            latest['change_pct'] = live_price_data.get('change_pct', 0)
            if live_price_data.get('volume'):
                latest['volume'] = live_price_data['volume']
            if live_price_data.get('high'):
                latest['high'] = max(latest.get('high', 0), live_price_data['high'])
            if live_price_data.get('low'):
                latest['low'] = min(latest.get('low', float('inf')), live_price_data['low'])
        
        # Check for signals (using potentially updated live price)
        signal = self._check_signals(symbol, df, latest, prev)
        
        return signal
    
    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators"""
        # EMA 20 and 50
        df['ema20'] = ema(df['close'], span=20)
        df['ema50'] = ema(df['close'], span=50)
        
        # RSI
        df['rsi'] = rsi(df['close'], n=14)
        
        # VWAP calculation
        df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
        
        # ATR calculation (simplified)
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            )
        )
        df['atr'] = df['tr'].rolling(window=14).mean()
        df['atr_pct'] = (df['atr'] / df['close']) * 100
        
        # Volume percentile
        df['vol_percentile'] = df['volume'].rank(pct=True) * 100
        
        return df
    
    def _check_signals(self, symbol: str, df: pd.DataFrame, latest: pd.Series, prev: pd.Series) -> Optional[Dict[str, Any]]:
        """Check for trading signals based on strategy logic"""
        reasons = []
        score = 0
        direction = None
        signal_type = "MOMENTUM"  # Default type
        
        # Calculate 52-week high/low
        week_52_high = df['high'].rolling(window=252, min_periods=50).max().iloc[-1]
        week_52_low = df['low'].rolling(window=252, min_periods=50).min().iloc[-1]
        
        # Calculate volume ratio
        avg_volume = df['volume'].mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 1.0
        
        # Calculate price change percentage
        price_change_pct = ((latest['close'] - prev['close']) / prev['close']) * 100
        
        # 1. VOLUME SHOCKER - Unusually high volume (3x+ average)
        if volume_ratio >= 3.0:
            signal_type = "VOLUME_SHOCKER"
            reasons.append(f"🔥 VOLUME SHOCKER: {volume_ratio:.1f}x average volume!")
            score += 35
            # Determine direction based on price action
            if price_change_pct > 0:
                direction = 'LONG'
                reasons.append(f"Price up {price_change_pct:+.1f}% with massive volume")
            else:
                direction = 'SHORT'
                reasons.append(f"Price down {price_change_pct:+.1f}% with massive volume")
        
        # 2. 52-WEEK HIGH BREAKOUT
        if latest['close'] >= week_52_high * 0.999:  # Within 0.1% of 52-week high
            signal_type = "52W_HIGH_BREAKOUT"
            reasons.append(f"🚀 52-WEEK HIGH BREAKOUT at ₹{week_52_high:.2f}")
            score += 40
            direction = 'LONG'
            if volume_ratio >= 1.5:
                reasons.append(f"Breakout with {volume_ratio:.1f}x volume confirmation")
                score += 15
        
        # 3. 52-WEEK LOW BREAKDOWN
        elif latest['close'] <= week_52_low * 1.001:  # Within 0.1% of 52-week low
            signal_type = "52W_LOW_BREAKDOWN"
            reasons.append(f"📉 52-WEEK LOW BREAKDOWN at ₹{week_52_low:.2f}")
            score += 40
            direction = 'SHORT'
            if volume_ratio >= 1.5:
                reasons.append(f"Breakdown with {volume_ratio:.1f}x volume confirmation")
                score += 15
        
        # 4. PRICE SHOCKER - Significant intraday move (5%+)
        if abs(price_change_pct) >= 5.0 and not signal_type.startswith("52W"):
            signal_type = "PRICE_SHOCKER"
            if price_change_pct > 0:
                reasons.append(f"⚡ PRICE SHOCKER: Up {price_change_pct:+.1f}% today!")
                direction = 'LONG'
            else:
                reasons.append(f"⚡ PRICE SHOCKER: Down {price_change_pct:+.1f}% today!")
                direction = 'SHORT'
            score += 30
            if volume_ratio >= 2.0:
                reasons.append(f"With {volume_ratio:.1f}x volume surge")
                score += 15
        
        # 5. MOMENTUM SIGNALS (Original logic)
        if not direction:  # Only check if no special signal detected
            # Opening Range Breakout
            opening_range_high = df.head(3)['high'].max()
            opening_range_low = df.head(3)['low'].min()
            
            # LONG Signal Checks
            if latest['close'] > opening_range_high:
                reasons.append(f"Breakout above opening range (₹{opening_range_high:.2f})")
                score += 20
                direction = 'LONG'
            
            # SHORT Signal Checks  
            elif latest['close'] < opening_range_low:
                reasons.append(f"Breakdown below opening range (₹{opening_range_low:.2f})")
                score += 20
                direction = 'SHORT'
            
            # If no breakout, check trend continuation
            if not direction:
                if latest['close'] > latest['ema20'] and latest['ema20'] > latest['ema50']:
                    direction = 'LONG'
                    reasons.append("Uptrend (Price > EMA20 > EMA50)")
                    score += 15
                elif latest['close'] < latest['ema20'] and latest['ema20'] < latest['ema50']:
                    direction = 'SHORT'
                    reasons.append("Downtrend (Price < EMA20 < EMA50)")
                    score += 15
        
        if not direction:
            return None
        
        # 2. VWAP Alignment
        if direction == 'LONG' and latest['close'] > latest['vwap']:
            reasons.append(f"Price above VWAP (₹{latest['vwap']:.2f})")
            score += 15
        elif direction == 'SHORT' and latest['close'] < latest['vwap']:
            reasons.append(f"Price below VWAP (₹{latest['vwap']:.2f})")
            score += 15
        
        # 3. Volume Expansion
        if latest['vol_percentile'] >= self.min_volume_percentile:
            volume_ratio = latest['volume'] / df['volume'].mean()
            reasons.append(f"{volume_ratio:.1f}x average volume")
            score += 20
        
        # 4. ATR Expansion (Volatility)
        if latest['atr_pct'] >= self.min_atr_pct:
            reasons.append(f"ATR {latest['atr_pct']:.1f}% (good volatility)")
            score += 10
        
        # 5. RSI Confirmation
        if direction == 'LONG' and 40 <= latest['rsi'] <= 70:
            reasons.append(f"RSI {latest['rsi']:.0f} (bullish zone)")
            score += 10
        elif direction == 'SHORT' and 30 <= latest['rsi'] <= 60:
            reasons.append(f"RSI {latest['rsi']:.0f} (bearish zone)")
            score += 10
        
        # 6. Momentum (price change)
        price_change_pct = ((latest['close'] - prev['close']) / prev['close']) * 100
        if abs(price_change_pct) > 0.5:
            reasons.append(f"Strong momentum ({price_change_pct:+.1f}%)")
            score += 10
        
        # Minimum score check
        if score < self.min_momentum_score:
            return None
        
        # Calculate entry, stop loss, and target
        entry_price = latest['close']
        atr = latest['atr']
        stop_loss = calculate_atr_stop_loss(entry_price, atr, direction, multiplier=1.5)
        target = calculate_target(entry_price, stop_loss, risk_reward_ratio=1.5)
        
        # Determine confidence level (more granular)
        if score >= 75:
            confidence = "HIGH"
        elif score >= 50:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        
        return {
            'instrument_type': 'STOCK',
            'symbol': symbol,
            'signal_type': signal_type,  # NEW: Type of signal
            'direction': direction,
            'timeframe': '5m',
            'momentum_score': min(score, 100),  # Cap at 100
            'confidence': confidence,
            'reasons': reasons,
            'entry_price': round(entry_price, 2),
            'stop_loss': round(stop_loss, 2),
            'target': round(target, 2),
            'current_price': round(latest['close'], 2),
            'atr': round(atr, 2),
            'rsi': round(latest['rsi'], 1),
            'volume': int(latest['volume']),
            'timestamp': datetime.now().isoformat()
        }
