"""
Black-Scholes Option Pricing Model
Calculate theoretical option prices for backtesting
"""

import numpy as np
import pandas as pd
from scipy.stats import norm
from typing import Tuple, Optional
from datetime import datetime, timedelta


def black_scholes_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Calculate Black-Scholes price for European Call Option
    
    Args:
        S: Current stock price
        K: Strike price
        T: Time to expiration (in years)
        r: Risk-free rate (annual)
        sigma: Volatility (annual)
    
    Returns:
        Call option price
    """
    if T <= 0:
        return max(S - K, 0)
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    call_price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return call_price


def black_scholes_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Calculate Black-Scholes price for European Put Option
    
    Args:
        S: Current stock price
        K: Strike price
        T: Time to expiration (in years)
        r: Risk-free rate (annual)
        sigma: Volatility (annual)
    
    Returns:
        Put option price
    """
    if T <= 0:
        return max(K - S, 0)
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    put_price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return put_price


def calculate_implied_volatility(
    option_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str = 'call',
    max_iterations: int = 100,
    tolerance: float = 1e-5
) -> float:
    """
    Calculate implied volatility using Newton-Raphson method
    
    Args:
        option_price: Market price of the option
        S: Current stock price
        K: Strike price
        T: Time to expiration (in years)
        r: Risk-free rate (annual)
        option_type: 'call' or 'put'
        max_iterations: Maximum iterations for convergence
        tolerance: Convergence tolerance
    
    Returns:
        Implied volatility (sigma)
    """
    # Initial guess
    sigma = 0.3
    
    for i in range(max_iterations):
        if option_type == 'call':
            price = black_scholes_call(S, K, T, r, sigma)
        else:
            price = black_scholes_put(S, K, T, r, sigma)
        
        diff = price - option_price
        
        if abs(diff) < tolerance:
            return sigma
        
        # Vega (derivative of option price with respect to sigma)
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        vega = S * norm.pdf(d1) * np.sqrt(T)
        
        if vega < 1e-10:
            break
        
        # Newton-Raphson update
        sigma = sigma - diff / vega
        
        # Keep sigma positive
        sigma = max(sigma, 0.01)
    
    return sigma


def calculate_atm_strike(spot_price: float, strike_interval: float = 50.0) -> float:
    """
    Calculate nearest ATM (At-The-Money) strike price
    
    Args:
        spot_price: Current spot price
        strike_interval: Strike price interval (e.g., 50, 100)
    
    Returns:
        ATM strike price
    """
    return round(spot_price / strike_interval) * strike_interval


def get_option_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = 'call'
) -> dict:
    """
    Calculate option Greeks
    
    Returns:
        Dictionary with delta, gamma, theta, vega, rho
    """
    if T <= 0:
        return {
            'delta': 1.0 if option_type == 'call' else -1.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0,
            'rho': 0.0
        }
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    # Delta
    if option_type == 'call':
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1
    
    # Gamma (same for call and put)
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    
    # Vega (same for call and put, divided by 100 for 1% change)
    vega = S * norm.pdf(d1) * np.sqrt(T) / 100
    
    # Theta
    if option_type == 'call':
        theta = (
            -S * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
            - r * K * np.exp(-r * T) * norm.cdf(d2)
        ) / 365
    else:
        theta = (
            -S * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
            + r * K * np.exp(-r * T) * norm.cdf(-d2)
        ) / 365
    
    # Rho (divided by 100 for 1% change in interest rate)
    if option_type == 'call':
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100
    
    return {
        'delta': delta,
        'gamma': gamma,
        'theta': theta,
        'vega': vega,
        'rho': rho
    }


# Default parameters for Indian market
DEFAULT_RISK_FREE_RATE = 0.065  # 6.5% (current RBI repo rate)
DEFAULT_VOLATILITY = 0.20  # 20% annual volatility (fallback)
DEFAULT_DTE = 7  # Days to expiration (weekly options)
NIFTY_DIVIDEND_YIELD = 0.012  # 1.2% annual dividend yield


def calculate_historical_volatility(
    price_data: pd.DataFrame,
    window: int = 20,
    timeframe_minutes: int = 5
) -> float:
    """
    Calculate annualized historical volatility from recent price data
    
    Args:
        price_data: DataFrame with 'close' column
        window: Lookback period (default: 20 periods)
        timeframe_minutes: Timeframe in minutes (default: 5 for 5-min data)
    
    Returns:
        Annualized volatility (e.g., 0.25 for 25%)
    """
    if len(price_data) < window + 1:
        return DEFAULT_VOLATILITY
    
    try:
        # Calculate log returns
        returns = np.log(price_data['close'] / price_data['close'].shift(1))
        
        # Calculate rolling standard deviation
        rolling_std = returns.rolling(window=window).std()
        
        # Get current volatility
        current_std = rolling_std.iloc[-1]
        
        if pd.isna(current_std) or current_std == 0:
            return DEFAULT_VOLATILITY
        
        # Annualize based on timeframe
        # For 5-min data: 78 periods per day (9:15 AM - 3:30 PM = 375 min / 5)
        periods_per_day = int(375 / timeframe_minutes)
        trading_days_per_year = 252
        annualization_factor = np.sqrt(periods_per_day * trading_days_per_year)
        
        annualized_vol = current_std * annualization_factor
        
        # Ensure reasonable bounds (10% to 60%)
        annualized_vol = max(min(annualized_vol, 0.60), 0.10)
        
        return annualized_vol
        
    except Exception as e:
        # Fallback to default if calculation fails
        return DEFAULT_VOLATILITY


def get_next_weekly_expiry(current_date: datetime) -> datetime:
    """
    Get next Thursday (weekly expiry for NIFTY options)
    
    Args:
        current_date: Current datetime
    
    Returns:
        Next Thursday at 3:30 PM (market close)
    """
    # Thursday = 3 (Monday = 0)
    days_ahead = 3 - current_date.weekday()
    
    if days_ahead <= 0:  # Already past or on Thursday
        days_ahead += 7
    
    expiry_date = current_date + timedelta(days=days_ahead)
    
    # Set to 3:30 PM (market close)
    expiry_datetime = expiry_date.replace(hour=15, minute=30, second=0, microsecond=0)
    
    return expiry_datetime


def calculate_time_to_expiry(current_time: datetime) -> float:
    """
    Calculate time to expiry in years (for Black-Scholes)
    
    Args:
        current_time: Current datetime during trading
    
    Returns:
        Time to expiry in years
    """
    expiry = get_next_weekly_expiry(current_time)
    
    # Calculate hours remaining
    time_delta = expiry - current_time
    hours_remaining = time_delta.total_seconds() / 3600
    
    # Convert to years (assuming 6.25 trading hours per day, 252 trading days)
    trading_hours_per_year = 6.25 * 252
    years_to_expiry = hours_remaining / trading_hours_per_year
    
    # Minimum 0.001 years (approx 6 trading hours) to avoid division by zero
    return max(years_to_expiry, 0.001)


def price_synthetic_option(
    underlying_price: float,
    option_type: str = 'CE',
    strike: float = None,
    days_to_expiry: int = None,
    volatility: float = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    historical_data: Optional[pd.DataFrame] = None,
    current_time: Optional[datetime] = None,
    apply_dividend_adjustment: bool = True
) -> float:
    """
    Price a synthetic option using Black-Scholes with improved accuracy
    
    Args:
        underlying_price: Current price of underlying stock
        option_type: 'CE' (call) or 'PE' (put)
        strike: Strike price (if None, uses ATM)
        days_to_expiry: Days until expiration (if None, calculates from current_time)
        volatility: Annual volatility (if None, calculates from historical_data)
        risk_free_rate: Risk-free rate (default: 6.5%)
        historical_data: DataFrame with price history for volatility calculation
        current_time: Current datetime for expiry calculation
        apply_dividend_adjustment: Whether to apply dividend yield adjustment
    
    Returns:
        Option price
    """
    # Calculate ATM strike if not provided
    if strike is None:
        strike = calculate_atm_strike(underlying_price)
    
    # Calculate dynamic volatility if not provided
    if volatility is None:
        if historical_data is not None and len(historical_data) > 20:
            volatility = calculate_historical_volatility(historical_data, window=20)
        else:
            volatility = DEFAULT_VOLATILITY
    
    # Calculate time to expiry
    if current_time is not None:
        T = calculate_time_to_expiry(current_time)
    elif days_to_expiry is not None:
        T = days_to_expiry / 365.0
    else:
        T = DEFAULT_DTE / 365.0
    
    # Calculate option price
    if option_type.upper() in ['CE', 'CALL']:
        price = black_scholes_call(
            underlying_price, strike, T, risk_free_rate, volatility
        )
        
        # Apply dividend adjustment (reduces call price)
        if apply_dividend_adjustment:
            price = price * (1 - NIFTY_DIVIDEND_YIELD * T)
            
    else:  # PE or PUT
        price = black_scholes_put(
            underlying_price, strike, T, risk_free_rate, volatility
        )
        
        # Apply dividend adjustment (increases put price)
        if apply_dividend_adjustment:
            price = price * (1 + NIFTY_DIVIDEND_YIELD * T)
    
    # Validate result - ensure no NaN or Inf values
    if not np.isfinite(price) or price <= 0:
        # Fallback to simple intrinsic value if calculation failed
        if option_type.upper() in ['CE', 'CALL']:
            price = max(underlying_price - strike, 1.0)  # Min 1 rupee
        else:
            price = max(strike - underlying_price, 1.0)  # Min 1 rupee
    
    return float(price)
