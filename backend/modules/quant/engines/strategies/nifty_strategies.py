
from typing import Dict, List, Any
from datetime import date
import pandas as pd
import numpy as np
from ..base_strategy import BaseStrategy

class NiftyVolContraction(BaseStrategy):
    """
    NIFTY Volatility Contraction Breakout.
    Signal: Long when ATR contracts and Price breaks out.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=100)
        
        if df.empty or len(df) < 60:
            return {"signal": 0}
            
        # Calc ATR
        df['tr'] = np.maximum(
            df['High'] - df['Low'],
            np.abs(df['High'] - df['Close'].shift(1))
        )
        df['atr'] = df['tr'].rolling(14).mean()
        avg_atr = df['atr'].rolling(60).mean()
        
        # Logic
        contracted = df['atr'].iloc[-1] < (avg_atr.iloc[-1] * 0.8)
        breakout = df['Close'].iloc[-1] > df['High'].rolling(20).max().shift(1).iloc[-1]
        
        signal = 1 if (contracted and breakout) else 0
        return {"signal": signal}

class NiftyDarvasBox(BaseStrategy):
    """
    NIFTY Darvas Box.
    Signal: Long on new 50-day high with volume.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=100)
        if df.empty or len(df) < 60: return {"signal": 0}

        high_50 = df['High'].rolling(50).max().shift(1)
        vol_ma = df['Volume'].rolling(20).mean()
        
        new_high = df['Close'].iloc[-1] > high_50.iloc[-1]
        vol_spike = df['Volume'].iloc[-1] > vol_ma.iloc[-1]
        
        return {"signal": 1 if new_high and vol_spike else 0}

class NiftyMtfTrend(BaseStrategy):
    """
    NIFTY Multi-Timeframe Trend.
    Signal: Long if > 150 SMA and momentum positive.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=200)
        if df.empty or len(df) < 160: return {"signal": 0}
        
        sma_150 = df['Close'].rolling(150).mean()
        trend_up = df['Close'].iloc[-1] > sma_150.iloc[-1]
        mom_20 = (df['Close'].iloc[-1] / df['Close'].iloc[-21]) - 1
        
        return {"signal": 1 if trend_up and mom_20 > 0.0 else 0}

class NiftyDualMa(BaseStrategy):
    """
    NIFTY Dual MA Crossover.
    Signal: Long if 50 EMA > 200 SMA.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=250)
        if df.empty or len(df) < 210: return {"signal": 0}
        
        ema_50 = df['Close'].ewm(span=50).mean()
        sma_200 = df['Close'].rolling(200).mean()
        
        return {"signal": 1 if ema_50.iloc[-1] > sma_200.iloc[-1] else 0}

class NiftyVolSpike(BaseStrategy):
    """
    NIFTY Volume Spike.
    Signal: Long if 90-day high and Vol > 1.5x Avg.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=120)
        if df.empty or len(df) < 100: return {"signal": 0}
        
        high_90 = df['High'].rolling(90).max().shift(1)
        vol_ma = df['Volume'].rolling(20).mean()
        
        breakout = df['Close'].iloc[-1] > high_90.iloc[-1]
        spike = df['Volume'].iloc[-1] > (vol_ma.iloc[-1] * 1.5)
        
        return {"signal": 1 if breakout and spike else 0}

class NiftyTrendEnvelope(BaseStrategy):
    """
    NIFTY Bollinger Breakout.
    Signal: Long if Close > Upper BB (20, 2).
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=50)
        if df.empty or len(df) < 25: return {"signal": 0}
        
        sma = df['Close'].rolling(20).mean()
        std = df['Close'].rolling(20).std()
        upper = sma + (2 * std)
        
        return {"signal": 1 if df['Close'].iloc[-1] > upper.iloc[-1] else 0}

class NiftyRegimeMom(BaseStrategy):
    """
    NIFTY Regime Mom.
    Signal: Long if > 200 SMA (Bull Regime) and RSI > 50.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=250)
        if df.empty or len(df) < 210: return {"signal": 0}
        
        sma_200 = df['Close'].rolling(200).mean()
        
        # Simple RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        bull_regime = df['Close'].iloc[-1] > sma_200.iloc[-1]
        mom_ok = rsi.iloc[-1] > 50
        
        return {"signal": 1 if bull_regime and mom_ok else 0}

class NiftyAtrBreak(BaseStrategy):
    """
    NIFTY ATR Breakout (Keltner-like).
    Signal: Long if > 20 SMA + 2 ATR.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=50)
        if df.empty or len(df) < 30: return {"signal": 0}

        df['tr'] = np.maximum(
            df['High'] - df['Low'],
            np.abs(df['High'] - df['Close'].shift(1))
        )
        atr = df['tr'].rolling(14).mean()
        
        sma_20 = df['Close'].rolling(20).mean()
        upper = sma_20 + (2 * atr)
        
        return {"signal": 1 if df['Close'].iloc[-1] > upper.iloc[-1] else 0}

class NiftyMaRibbon(BaseStrategy):
    """
    NIFTY MA Ribbon.
    Signal: Long if 20 > 50 > 100 > 200.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=250)
        if df.empty or len(df) < 210: return {"signal": 0}
        
        ma20 = df['Close'].rolling(20).mean().iloc[-1]
        ma50 = df['Close'].rolling(50).mean().iloc[-1]
        ma100 = df['Close'].rolling(100).mean().iloc[-1]
        ma200 = df['Close'].rolling(200).mean().iloc[-1]
        
        aligned = (ma20 > ma50) and (ma50 > ma100) and (ma100 > ma200)
        return {"signal": 1 if aligned else 0}

class NiftyMacroBreak(BaseStrategy):
    """
    NIFTY Macro Breakout.
    Signal: Long if > 252-day High.
    """
    def run_day(self, current_date: date, symbols: List[str], data_provider) -> Dict[str, Any]:
        symbol = symbols[0]
        df = data_provider.get_history(symbol, timeframe="1D", end_date=current_date, days=300)
        if df.empty or len(df) < 260: return {"signal": 0}
        
        high_year = df['High'].rolling(252).max().shift(1)
        return {"signal": 1 if df['Close'].iloc[-1] > high_year.iloc[-1] else 0}
