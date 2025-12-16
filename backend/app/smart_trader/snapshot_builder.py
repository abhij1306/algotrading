"""
Utility to build MarketSnapshot from historical data with computed indicators
"""
from typing import Optional
from datetime import datetime
import pandas as pd
import numpy as np

from .models import MarketSnapshot


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Compute Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Relative Strength Index"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range"""
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


class SnapshotBuilder:
    """Builds MarketSnapshot objects from historical price data"""
    
    @staticmethod
    def from_dataframe(
        symbol: str,
        df: pd.DataFrame,
        timeframe: str = "5m",
        nifty_change_pct: Optional[float] = None
    ) -> Optional[MarketSnapshot]:
        """
        Build MarketSnapshot from pandas DataFrame with computed indicators.
        
        Args:
            symbol: Stock symbol
            df: DataFrame with OHLCV data
            timeframe: Timeframe string
            nifty_change_pct: Optional Nifty change percentage
            
        Returns:
            MarketSnapshot or None if insufficient data
        """
        if df is None or df.empty or len(df) < 50:
            print(f"⚠️ {symbol}: Insufficient data ({len(df) if df is not None else 0} rows)")
            return None
        
        try:
            # Ensure required columns exist
            required_cols = ['open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_cols):
                print(f"⚠️ {symbol}: Missing required columns")
                return None
            
            # Compute technical indicators
            df['ema_9'] = compute_ema(df['close'], 9)
            df['ema_21'] = compute_ema(df['close'], 21)
            df['ema_50'] = compute_ema(df['close'], 50)
            df['rsi'] = compute_rsi(df['close'], 14)
            df['atr'] = compute_atr(df, 14)
            
            # Volume metrics
            df['avg_volume'] = df['volume'].rolling(window=20).mean()
            
            # Get latest and previous rows
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else None
            
            # Calculate volume ratio
            volume_ratio = None
            if pd.notna(latest.get('avg_volume')) and latest.get('avg_volume', 0) > 0:
                volume_ratio = float(latest['volume'] / latest['avg_volume'])
            
            # Calculate ATR percentage
            atr_pct = None
            if pd.notna(latest.get('atr')) and latest['close'] > 0:
                atr_pct = (float(latest['atr']) / float(latest['close'])) * 100
            
            # Calculate day change
            day_change_pct = 0.0
            if prev is not None and prev['close'] > 0:
                day_change_pct = ((float(latest['close']) - float(prev['close'])) / float(prev['close'])) * 100
            
            # Calculate trend strength
            trend_strength = SnapshotBuilder._calculate_trend_strength(df)
            
            # Build snapshot
            snapshot = MarketSnapshot(
                symbol=symbol,
                timestamp=datetime.now(),
                timeframe=timeframe,
                
                # Price data (required)
                open=float(latest['open']),
                high=float(latest['high']),
                low=float(latest['low']),
                close=float(latest['close']),
                volume=int(latest['volume']),
                
                # Technical indicators
                ema_9=float(latest['ema_9']) if pd.notna(latest.get('ema_9')) else None,
                ema_21=float(latest['ema_21']) if pd.notna(latest.get('ema_21')) else None,
                ema_50=float(latest['ema_50']) if pd.notna(latest.get('ema_50')) else None,
                rsi=float(latest['rsi']) if pd.notna(latest.get('rsi')) else None,
                atr=float(latest['atr']) if pd.notna(latest.get('atr')) else None,
                
                # Volume metrics
                avg_volume_20=float(latest['avg_volume']) if pd.notna(latest.get('avg_volume')) else None,
                volume_ratio=volume_ratio,
                
                # Trend metrics
                trend_strength=trend_strength,
                volatility=atr_pct,
                
                # Index context
                nifty_change_pct=nifty_change_pct,
                correlation_with_nifty=None,
                relative_strength=None,
                
                # Recent behavior
                prev_close=float(prev['close']) if prev is not None else None,
                day_high=float(df['high'].tail(20).max()),
                day_low=float(df['low'].tail(20).min()),
                day_change_pct=day_change_pct,
                
                # Metadata
                metadata={}
            )
            
            # Log snapshot creation
            missing_fields = []
            if snapshot.ema_9 is None: missing_fields.append('ema_9')
            if snapshot.rsi is None: missing_fields.append('rsi')
            if snapshot.atr is None: missing_fields.append('atr')
            
            if missing_fields:
                print(f"📊 {symbol}: Created snapshot (missing: {', '.join(missing_fields)})")
            else:
                print(f"✅ {symbol}: Created complete snapshot")
            
            return snapshot
        
        except Exception as e:
            print(f"❌ {symbol}: Error building snapshot: {e}")
            return None
    
    @staticmethod
    def _calculate_trend_strength(df: pd.DataFrame) -> Optional[float]:
        """Calculate trend strength (0-1) based on EMA alignment"""
        if len(df) < 2:
            return None
        
        latest = df.iloc[-1]
        
        # Check if we have EMAs
        if 'ema_9' not in latest or 'ema_21' not in latest:
            return None
        
        ema_9 = latest.get('ema_9')
        ema_21 = latest.get('ema_21')
        ema_50 = latest.get('ema_50')
        
        if pd.isna(ema_9) or pd.isna(ema_21):
            return None
        
        # Calculate alignment score
        score = 0.0
        
        # EMA 9 vs 21
        if ema_9 > ema_21:
            score += 0.4
        
        # EMA 21 vs 50 (if available)
        if ema_50 and not pd.isna(ema_50):
            if ema_21 > ema_50:
                score += 0.3
        
        # Price vs EMA 21
        if latest['close'] > ema_21:
            score += 0.3
        
        return min(score, 1.0)
