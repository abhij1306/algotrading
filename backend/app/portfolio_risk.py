"""
Portfolio Risk Analytics Engine
Hedge-fund style risk analysis for personal portfolios
Includes VaR, CVaR, Monte Carlo simulation, correlation analysis
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from scipy import stats
import json


class PortfolioRiskEngine:
    """
    Comprehensive portfolio risk analysis engine
    Calculates market risk, portfolio risk, and runs Monte Carlo simulations
    """
    
    def __init__(self):
        self.trading_days_per_year = 252
        self.risk_free_rate = 0.07  # 7% annual risk-free rate (India)
    
    # ==================== MARKET RISK METRICS ====================
    
    def calculate_returns(self, prices) -> pd.DataFrame:
        """
        Calculate log returns from price data
        Args:
            prices: DataFrame or Series with price data
        Returns:
            DataFrame of log returns
        """
        # Convert Series to DataFrame if needed
        if isinstance(prices, pd.Series):
            prices = prices.to_frame()
        
        return np.log(prices / prices.shift(1)).dropna()
    
    def portfolio_volatility(self, weights: np.array, cov_matrix: np.array) -> float:
        """
        Calculate portfolio volatility (annualized)
        Args:
            weights: Array of portfolio weights
            cov_matrix: Covariance matrix of returns
        Returns:
            Annualized portfolio volatility
        """
        portfolio_variance = np.dot(weights.T, np.dot(cov_matrix, weights))
        return np.sqrt(portfolio_variance * self.trading_days_per_year)
    
    def value_at_risk(self, returns: pd.Series, confidence: float = 0.95) -> float:
        """
        Calculate Value at Risk (VaR) at given confidence level
        Args:
            returns: Series of portfolio returns
            confidence: Confidence level (default 95%)
        Returns:
            VaR as a percentage
        """
        return np.percentile(returns, (1 - confidence) * 100)
    
    def conditional_var(self, returns: pd.Series, confidence: float = 0.95) -> float:
        """
        Calculate Conditional VaR (Expected Shortfall)
        Average of returns worse than VaR
        Args:
            returns: Series of portfolio returns
            confidence: Confidence level (default 95%)
        Returns:
            CVaR as a percentage
        """
        var = self.value_at_risk(returns, confidence)
        return returns[returns <= var].mean()
    
    def max_drawdown(self, prices: pd.Series) -> Dict:
        """
        Calculate maximum drawdown and related metrics
        Args:
            prices: Series of portfolio values
        Returns:
            Dict with max_drawdown, peak_date, trough_date, recovery_date
        """
        cumulative = (1 + prices).cumprod()
        running_max = cumulative.cummax()
        drawdown = (cumulative - running_max) / running_max
        
        max_dd = drawdown.min()
        max_dd_idx = drawdown.idxmin()
        
        # Get integer position of max drawdown
        try:
            max_dd_pos = drawdown.index.get_loc(max_dd_idx)
        except (KeyError, ValueError, AttributeError):
            # Fallback if index isn't unique or standard
            max_dd_pos = len(drawdown) - 1

        # Find peak before max drawdown
        # Slice by position up to max drawdown
        peak_idx = cumulative.iloc[:max_dd_pos].idxmax()
        
        # Find recovery (if any)
        recovery_idx = None
        if max_dd_pos < len(cumulative) - 1:
            future = cumulative.iloc[max_dd_pos:]
            peak_value = cumulative[peak_idx]
            recovery = future[future >= peak_value]
            if len(recovery) > 0:
                recovery_idx = recovery.index[0]
        
        return {
            'max_drawdown': max_dd,
            'peak_date': peak_idx,
            'trough_date': max_dd_idx,
            'recovery_date': recovery_idx,
            'recovery_days': (recovery_idx - max_dd_idx).days if recovery_idx else None
        }
    
    def calculate_beta(self, asset_returns: pd.Series, market_returns: pd.Series) -> float:
        """
        Calculate beta (sensitivity to market)
        Args:
            asset_returns: Portfolio returns
            market_returns: Market (NIFTY50) returns
        Returns:
            Beta value
        """
        covariance = np.cov(asset_returns, market_returns)[0][1]
        market_variance = np.var(market_returns)
        return covariance / market_variance if market_variance != 0 else 1.0
    
    def sharpe_ratio(self, returns: pd.Series) -> float:
        """
        Calculate Sharpe ratio (risk-adjusted return)
        Args:
            returns: Series of returns
        Returns:
            Annualized Sharpe ratio
        """
        excess_returns = returns.mean() - (self.risk_free_rate / self.trading_days_per_year)
        return (excess_returns / returns.std()) * np.sqrt(self.trading_days_per_year)
    
    # ==================== PORTFOLIO RISK METRICS ====================
    
    def marginal_risk_contribution(self, weights: np.array, cov_matrix: np.array) -> np.array:
        """
        Calculate each position's marginal contribution to portfolio risk
        Args:
            weights: Array of portfolio weights
            cov_matrix: Covariance matrix
        Returns:
            Array of marginal risk contributions
        """
        portfolio_vol = self.portfolio_volatility(weights, cov_matrix)
        marginal_contrib = np.dot(cov_matrix, weights) / portfolio_vol
        return marginal_contrib * weights  # Component contribution
    
    def concentration_risk(self, weights: np.array) -> float:
        """
        Calculate concentration risk using Herfindahl index
        Args:
            weights: Array of portfolio weights
        Returns:
            Herfindahl index (0 to 1, higher = more concentrated)
        """
        return np.sum(weights ** 2)
    
    def correlation_matrix(self, returns: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate correlation matrix for portfolio stocks
        Args:
            returns: DataFrame of returns (columns = stocks)
        Returns:
            Correlation matrix
        """
        return returns.corr()
    
    # ==================== MONTE CARLO SIMULATION ====================
    
    def monte_carlo_simulation(
        self,
        returns: pd.DataFrame,
        weights: np.array,
        time_horizon_days: int = 252,
        num_simulations: int = 10000
    ) -> Dict:
        """
        Run Monte Carlo simulation for portfolio returns
        Args:
            returns: DataFrame of historical returns
            weights: Portfolio weights
            time_horizon_days: Forecast horizon in trading days
            num_simulations: Number of simulation paths
        Returns:
            Dict with simulation results
        """
        # Calculate mean returns and covariance
        mean_returns = returns.mean().values
        cov_matrix = returns.cov().values
        
        # Initialize results array
        simulation_results = np.zeros(num_simulations)
        
        # Run simulations
        for i in range(num_simulations):
            # Generate random returns using multivariate normal distribution
            simulated_returns = np.random.multivariate_normal(
                mean_returns,
                cov_matrix,
                time_horizon_days
            )
            
            # Calculate portfolio returns for each day
            portfolio_returns = np.dot(simulated_returns, weights)
            
            # Calculate cumulative return
            cumulative_return = np.prod(1 + portfolio_returns) - 1
            simulation_results[i] = cumulative_return
        
        # Calculate statistics
        return {
            'mean_return': np.mean(simulation_results),
            'median_return': np.median(simulation_results),
            'std_return': np.std(simulation_results),
            'percentile_5': np.percentile(simulation_results, 5),
            'percentile_25': np.percentile(simulation_results, 25),
            'percentile_50': np.percentile(simulation_results, 50),
            'percentile_75': np.percentile(simulation_results, 75),
            'percentile_95': np.percentile(simulation_results, 95),
            'prob_loss': np.sum(simulation_results < 0) / num_simulations,
            'prob_loss_10pct': np.sum(simulation_results < -0.10) / num_simulations,
            'prob_loss_20pct': np.sum(simulation_results < -0.20) / num_simulations,
            'all_results': simulation_results.tolist()
        }
    
    # ==================== FUNDAMENTAL RISK METRICS ====================
    
    def leverage_score(self, debt_to_equity_ratios: List[float], weights: np.array) -> float:
        """
        Calculate weighted average leverage score
        Args:
            debt_to_equity_ratios: List of D/E ratios for each stock
            weights: Portfolio weights
        Returns:
            Weighted average D/E ratio
        """
        return np.dot(debt_to_equity_ratios, weights)
    
    def financial_fragility_score(self, financials: Dict) -> float:
        """
        Calculate financial fragility score (0-100, lower is better)
        Based on: leverage, liquidity, profitability, cash flow
        Args:
            financials: Dict with financial metrics
        Returns:
            Fragility score (0-100)
        """
        score = 0
        
        # Leverage component (0-30 points)
        de_ratio = financials.get('debt_to_equity', 0)
        if de_ratio > 2.0:
            score += 30
        elif de_ratio > 1.0:
            score += 20
        elif de_ratio > 0.5:
            score += 10
        
        # Profitability component (0-25 points)
        roe = financials.get('roe', 0)
        if roe < 5:
            score += 25
        elif roe < 10:
            score += 15
        elif roe < 15:
            score += 5
        
        # Liquidity component (0-25 points)
        current_ratio = financials.get('current_ratio', 1.0)
        if current_ratio < 0.5:
            score += 25
        elif current_ratio < 1.0:
            score += 15
        elif current_ratio < 1.5:
            score += 5
        
        # Cash flow component (0-20 points)
        fcf = financials.get('free_cash_flow', 0)
        if fcf < 0:
            score += 20
        elif fcf < 100:  # Assuming in Crores
            score += 10
        
        return min(score, 100)
    
    # ==================== COMPREHENSIVE ANALYSIS ====================
    
    def analyze_portfolio(
        self,
        prices: pd.DataFrame,
        weights: np.array,
        market_prices: pd.Series,
        financials: List[Dict],
        lookback_days: int = 252
    ) -> Dict:
        """
        Comprehensive portfolio risk analysis
        Args:
            prices: DataFrame of stock prices (columns = symbols)
            weights: Portfolio weights
            market_prices: NIFTY50 prices for beta calculation
            financials: List of financial dicts for each stock
            lookback_days: Historical data period
        Returns:
            Complete risk analysis dict
        """
        # Calculate returns
        returns = self.calculate_returns(prices)
        market_returns_df = self.calculate_returns(market_prices)
        
        # Extract market returns as Series (calculate_beta expects Series)
        market_returns = market_returns_df.iloc[:, 0] if isinstance(market_returns_df, pd.DataFrame) else market_returns_df
        
        # Portfolio returns
        portfolio_returns = (returns * weights).sum(axis=1)
        
        # Covariance matrix
        cov_matrix = returns.cov().values
        
        # Market risk metrics
        market_risk = {
            'volatility': returns.std().mean() * np.sqrt(self.trading_days_per_year),
            'portfolio_volatility': self.portfolio_volatility(weights, cov_matrix),
            'var_95': self.value_at_risk(portfolio_returns, 0.95),
            'var_99': self.value_at_risk(portfolio_returns, 0.99),
            'cvar_95': self.conditional_var(portfolio_returns, 0.95),
            'cvar_99': self.conditional_var(portfolio_returns, 0.99),
            'beta': self.calculate_beta(portfolio_returns, market_returns),
            'sharpe_ratio': self.sharpe_ratio(portfolio_returns),
            'max_drawdown': self.max_drawdown(portfolio_returns)
        }
        
        # Portfolio risk metrics
        portfolio_risk = {
            'concentration': self.concentration_risk(weights),
            'marginal_contributions': self.marginal_risk_contribution(weights, cov_matrix).tolist(),
            'correlation_matrix': self.correlation_matrix(returns).to_dict()
        }
        
        # Fundamental risk
        de_ratios = [f.get('debt_to_equity', 0) for f in financials]
        fundamental_risk = {
            'avg_leverage': self.leverage_score(de_ratios, weights),
            'fragility_scores': [self.financial_fragility_score(f) for f in financials],
            'avg_fragility': np.average([self.financial_fragility_score(f) for f in financials], weights=weights)
        }
        
        return {
            'market_risk': market_risk,
            'portfolio_risk': portfolio_risk,
            'fundamental_risk': fundamental_risk,
            'timestamp': datetime.now().isoformat()
        }
