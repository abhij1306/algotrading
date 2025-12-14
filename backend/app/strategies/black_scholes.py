"""
Black-Scholes Option Pricing Model
Calculate theoretical option prices for backtesting
"""

import numpy as np
from scipy.stats import norm
from typing import Tuple


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
DEFAULT_RISK_FREE_RATE = 0.07  # 7% (approx RBI repo rate)
DEFAULT_VOLATILITY = 0.25  # 25% annual volatility
DEFAULT_DTE = 30  # Days to expiration


def price_synthetic_option(
    underlying_price: float,
    option_type: str = 'CE',
    strike: float = None,
    days_to_expiry: int = DEFAULT_DTE,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
) -> float:
    """
    Price a synthetic option using Black-Scholes
    
    Args:
        underlying_price: Current price of underlying stock
        option_type: 'CE' (call) or 'PE' (put)
        strike: Strike price (if None, uses ATM)
        days_to_expiry: Days until expiration
        volatility: Annual volatility (default: 25%)
        risk_free_rate: Risk-free rate (default: 7%)
    
    Returns:
        Option price
    """
    # Calculate ATM strike if not provided
    if strike is None:
        strike = calculate_atm_strike(underlying_price)
    
    # Convert days to years
    T = days_to_expiry / 365.0
    
    # Calculate option price
    if option_type.upper() in ['CE', 'CALL']:
        price = black_scholes_call(
            underlying_price, strike, T, risk_free_rate, volatility
        )
    else:  # PE or PUT
        price = black_scholes_put(
            underlying_price, strike, T, risk_free_rate, volatility
        )
    
    return price
