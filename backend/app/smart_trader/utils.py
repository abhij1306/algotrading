"""
Utility functions for Smart Trader
"""
from datetime import datetime, time
from typing import List, Dict, Any
import pytz


def is_market_hours(market_config: Dict[str, str]) -> bool:
    """Check if current time is within market hours"""
    tz = pytz.timezone(market_config.get('timezone', 'Asia/Kolkata'))
    now = datetime.now(tz)
    
    start_time = time.fromisoformat(market_config.get('start', '09:15'))
    end_time = time.fromisoformat(market_config.get('end', '15:30'))
    
    return start_time <= now.time() <= end_time


def is_market_open(market_config: Dict[str, str]) -> bool:
    """Check if market is open (includes weekday check)"""
    tz = pytz.timezone(market_config.get('timezone', 'Asia/Kolkata'))
    now = datetime.now(tz)
    
    # Check if it's a weekday (Monday=0, Sunday=6)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    
    return is_market_hours(market_config)


def calculate_position_size(capital: float, risk_pct: float, entry: float, stop_loss: float) -> int:
    """Calculate position size based on risk amount"""
    risk_amount = capital * (risk_pct / 100)
    price_risk = abs(entry - stop_loss)
    
    if price_risk == 0:
        return 0
    
    quantity = int(risk_amount / price_risk)
    return quantity


def calculate_atr_stop_loss(entry_price: float, atr: float, direction: str, multiplier: float = 1.5) -> float:
    """Calculate ATR-based stop loss"""
    if direction.upper() == 'LONG':
        return entry_price - (atr * multiplier)
    else:  # SHORT
        return entry_price + (atr * multiplier)


def calculate_target(entry_price: float, stop_loss: float, risk_reward_ratio: float = 1.5) -> float:
    """Calculate target price based on risk-reward ratio"""
    risk = abs(entry_price - stop_loss)
    reward = risk * risk_reward_ratio
    
    if entry_price > stop_loss:  # LONG
        return entry_price + reward
    else:  # SHORT
        return entry_price - reward


def format_signal_reasons(reasons: List[str]) -> str:
    """Format signal reasons as bullet points"""
    return "\n".join([f"• {reason}" for reason in reasons])


def calculate_slippage(price: float, slippage_pct: float, direction: str) -> float:
    """Calculate realistic slippage"""
    slippage = price * (slippage_pct / 100)
    
    if direction.upper() == 'LONG':
        return price + slippage
    else:  # SHORT
        return price - slippage


def get_lot_size(symbol: str) -> int:
    """Get lot size for F&O instruments - NSE Dec 2024 specifications"""
    # Normalize symbol
    symbol = symbol.upper().replace('NSE:', '')
    
    # Correct lot sizes as per NSE Dec 2024 (post Nov 20, 2024 revision)
    lot_sizes = {
        # Index Options (NEW lot sizes from Nov 20, 2024)
        'NIFTY': 75,          # Changed to 75 from Nov 20, 2024
        'BANKNIFTY': 30,      # Changed to 30 from Nov 20, 2024
        'FINNIFTY': 40,
        'MIDCPNIFTY': 75,
        'NIFTYIT': 15,
        'SENSEX': 10,
        
        # Popular F&O Stocks - Corrected lot sizes
        'RELIANCE': 500,      # Changed to 500 after bonus issue (Oct 2024)
        'MARUTI': 50,         # Correct: 50 (not 100)
        'TCS': 175,
        'INFY': 400,
        'HDFCBANK': 550,
        'ICICIBANK': 1400,
        'HINDUNILVR': 300,
        'ITC': 1600,
        'SBIN': 1500,
        'BHARTIARTL': 950,
        'KOTAKBANK': 400,
        'LT': 75,
        'AXISBANK': 1200,
        'ASIANPAINT': 300,
        'BAJFINANCE': 125,
        'TITAN': 375,
        'NESTLEIND': 50,
        'ULTRACEMCO': 100,
        'TATASTEEL': 5500,
        'WIPRO': 1500,
        'POWERGRID': 4500,
        'NTPC': 2925,
        'SUNPHARMA': 350,
        'TECHM': 600,
        'M&M': 350,
        'TATAMOTORS': 1400,
        'ONGC': 3850,
        'COALINDIA': 2100,
        'HCLTECH': 350,
        'ADANIENT': 500,
        'ADANIPORTS': 1250,
        'JSWSTEEL': 675,
        'GRASIM': 475,
        'DRREDDY': 125,
        'CIPLA': 650,
        'BAJAJFINSV': 500,
        'APOLLOHOSP': 125,
        'EICHERMOT': 175,
        'DIVISLAB': 175,
        'HEROMOTOCO': 150,
        'BRITANNIA': 200,
        'HINDALCO': 1400,
        'SBILIFE': 750,
        'TATACONSUM': 550,
        'INDUSINDBK': 900,
        'BPCL': 1800,
        'BAJAJ-AUTO': 250,
        'HDFCLIFE': 1100,
        'UPL': 1300,
        'VEDL': 3100,
        'TRENT': 150,
        'BEL': 3500,
        'ZOMATO': 5000,
        'JIOFIN': 1500,
        'SHRIRAMFIN': 300,
        'HAL': 25,
        'PFC': 2200,
        'RECLTD': 1800,
        'TATAPOWER': 2700,
        'IOC': 4875,
        'GAIL': 4575,
    }
    
    # Direct match
    if symbol in lot_sizes:
        return lot_sizes[symbol]
    
    # Partial match for indices
    for key in ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']:
        if key in symbol:
            return lot_sizes[key]
    
    # Default for unknown symbols - assume equity (qty calculated by risk)
    return 1


def round_to_lot_size(quantity: int, lot_size: int) -> int:
    """Round quantity to nearest lot size"""
    if lot_size <= 1:
        return quantity
    
    return (quantity // lot_size) * lot_size


def calculate_pnl(entry_price: float, exit_price: float, quantity: int, direction: str, commission: float = 0) -> float:
    """Calculate P&L for a trade"""
    if direction.upper() == 'LONG':
        pnl = (exit_price - entry_price) * quantity
    else:  # SHORT
        pnl = (entry_price - exit_price) * quantity
    
    return pnl - commission


def get_nse_fo_universe() -> List[str]:
    """Get list of NSE F&O stocks"""
    # This is a sample list - in production, fetch from Fyers API or CSV
    return [
        'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK',
        'HINDUNILVR', 'ITC', 'SBIN', 'BHARTIARTL', 'KOTAKBANK',
        'LT', 'AXISBANK', 'ASIANPAINT', 'MARUTI', 'TITAN',
        'WIPRO', 'ULTRACEMCO', 'TATASTEEL', 'TECHM', 'HCLTECH'
    ]
