"""
Risk Metrics Calculation Engine
Combines technical (market) and fundamental (financial) risk metrics
for institutional-grade portfolio risk assessment.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta


class RiskMetricsEngine:
    """Calculate comprehensive risk metrics for portfolio analysis"""
    
    def __init__(self):
        self.trading_days_per_year = 252
        self.risk_free_rate = 0.07  # 7% annual risk-free rate (India)
    
    # ==================== TECHNICAL RISK METRICS ====================
    
    def calculate_returns(self, prices: pd.Series) -> pd.Series:
        """Calculate log returns from price series"""
        return np.log(prices / prices.shift(1)).dropna()
    
    def sharpe_ratio(self, returns: pd.Series) -> float:
        """Sharpe Ratio: (Return - RiskFree) / Volatility"""
        if len(returns) < 2:
            return 0.0
        
        mean_return = returns.mean() * self.trading_days_per_year
        volatility = returns.std() * np.sqrt(self.trading_days_per_year)
        
        if volatility == 0:
            return 0.0
        
        return (mean_return - self.risk_free_rate) / volatility
    
    def sortino_ratio(self, returns: pd.Series) -> float:
        """Sortino Ratio: Only penalizes downside volatility"""
        if len(returns) < 2:
            return 0.0
        
        mean_return = returns.mean() * self.trading_days_per_year
        downside_returns = returns[returns < 0]
        
        if len(downside_returns) == 0:
            return float('inf')
        
        downside_std = downside_returns.std() * np.sqrt(self.trading_days_per_year)
        
        if downside_std == 0:
            return 0.0
        
        return (mean_return - self.risk_free_rate) / downside_std
    
    def max_drawdown(self, prices: pd.Series) -> float:
        """Maximum Drawdown: Largest peak-to-trough decline"""
        cumulative = (1 + self.calculate_returns(prices)).cumprod()
        running_max = cumulative.expanding().max()
        drawdown = (cumulative - running_max) / running_max
        return drawdown.min()
    
    def value_at_risk(self, returns: pd.Series, confidence: float = 0.95) -> float:
        """Value at Risk at given confidence level"""
        if len(returns) < 10:
            return 0.0
        return np.percentile(returns, (1 - confidence) * 100)
    
    def conditional_var(self, returns: pd.Series, confidence: float = 0.95) -> float:
        """Conditional VaR (Expected Shortfall): Average of worst cases"""
        var = self.value_at_risk(returns, confidence)
        return returns[returns <= var].mean()
    
    def beta(self, asset_returns: pd.Series, market_returns: pd.Series) -> float:
        """Beta: Sensitivity to market movements"""
        if len(asset_returns) < 10 or len(market_returns) < 10:
            return 1.0
        
        # Align the series
        aligned = pd.DataFrame({
            'asset': asset_returns,
            'market': market_returns
        }).dropna()
        
        if len(aligned) < 10:
            return 1.0
        
        covariance = aligned['asset'].cov(aligned['market'])
        market_variance = aligned['market'].var()
        
        if market_variance == 0:
            return 1.0
        
        return covariance / market_variance
    
    def annualized_volatility(self, returns: pd.Series) -> float:
        """Annualized volatility (standard deviation)"""
        if len(returns) < 2:
            return 0.0
        return returns.std() * np.sqrt(self.trading_days_per_year)
    
    # ==================== FUNDAMENTAL RISK METRICS ====================
    
    def debt_to_equity(self, total_debt: float, total_equity: float) -> float:
        """Debt-to-Equity Ratio: Financial leverage"""
        if total_equity == 0:
            return float('inf')
        return total_debt / total_equity
    
    def interest_coverage(self, ebit: float, interest_expense: float) -> float:
        """Interest Coverage Ratio: Ability to pay interest"""
        if interest_expense == 0:
            return float('inf')
        return ebit / interest_expense
    
    def current_ratio(self, current_assets: float, current_liabilities: float) -> float:
        """Current Ratio: Short-term liquidity"""
        if current_liabilities == 0:
            return float('inf')
        return current_assets / current_liabilities
    
    def roe(self, net_income: float, shareholders_equity: float) -> float:
        """Return on Equity"""
        if shareholders_equity == 0:
            return 0.0
        return net_income / shareholders_equity
    
    def roa(self, net_income: float, total_assets: float) -> float:
        """Return on Assets"""
        if total_assets == 0:
            return 0.0
        return net_income / total_assets
    
    def profit_margin(self, net_income: float, revenue: float) -> float:
        """Net Profit Margin"""
        if revenue == 0:
            return 0.0
        return net_income / revenue
    
    def revenue_growth_volatility(self, revenues: List[float]) -> float:
        """Volatility of revenue growth (instability indicator)"""
        if len(revenues) < 3:
            return 0.0
        
        growth_rates = []
        for i in range(1, len(revenues)):
            if revenues[i-1] != 0:
                growth_rates.append((revenues[i] - revenues[i-1]) / revenues[i-1])
        
        if len(growth_rates) < 2:
            return 0.0
        
        return np.std(growth_rates)
    
    # ==================== RISK SCORING ====================
    
    def technical_risk_score(self, metrics: Dict) -> float:
        """
        Calculate technical risk score (0-10, higher is better)
        Based on: Sharpe, Sortino, Max Drawdown, VaR, Volatility
        """
        score = 5.0  # Start at neutral
        
        # Sharpe Ratio contribution (0-2 points)
        sharpe = metrics.get('sharpe_ratio', 0)
        if sharpe > 2.0:
            score += 2.0
        elif sharpe > 1.0:
            score += 1.5
        elif sharpe > 0.5:
            score += 1.0
        elif sharpe < 0:
            score -= 1.0
        
        # Max Drawdown contribution (0-2 points)
        mdd = abs(metrics.get('max_drawdown', 0))
        if mdd < 0.10:  # Less than 10%
            score += 2.0
        elif mdd < 0.20:
            score += 1.0
        elif mdd > 0.40:
            score -= 2.0
        
        # VaR contribution (0-2 points)
        var_95 = abs(metrics.get('var_95', 0))
        if var_95 < 0.02:  # Less than 2% daily VaR
            score += 2.0
        elif var_95 < 0.03:
            score += 1.0
        elif var_95 > 0.05:
            score -= 1.0
        
        # Volatility contribution (0-2 points)
        vol = metrics.get('volatility', 0)
        if vol < 0.15:  # Less than 15% annual vol
            score += 2.0
        elif vol < 0.25:
            score += 1.0
        elif vol > 0.40:
            score -= 1.0
        
        # Beta contribution (0-2 points)
        beta_val = metrics.get('beta', 1.0)
        if 0.8 <= beta_val <= 1.2:  # Near market beta
            score += 1.0
        elif beta_val > 1.5:  # High systematic risk
            score -= 1.0
        
        return max(0.0, min(10.0, score))
    
    def fundamental_risk_score(self, metrics: Dict) -> float:
        """
        Calculate fundamental risk score (0-10, higher is better)
        Based on: Debt/Equity, ROE, Profit Margin, Liquidity
        """
        score = 5.0  # Start at neutral
        
        # Debt-to-Equity contribution (0-3 points)
        de_ratio = metrics.get('debt_equity', 0)
        if de_ratio < 0.3:
            score += 3.0
        elif de_ratio < 0.5:
            score += 2.0
        elif de_ratio < 1.0:
            score += 1.0
        elif de_ratio > 2.0:
            score -= 2.0
        
        # ROE contribution (0-2 points)
        roe_val = metrics.get('roe', 0)
        if roe_val > 0.20:  # >20% ROE
            score += 2.0
        elif roe_val > 0.15:
            score += 1.5
        elif roe_val > 0.10:
            score += 1.0
        elif roe_val < 0.05:
            score -= 1.0
        
        # Profit Margin contribution (0-2 points)
        margin = metrics.get('profit_margin', 0)
        if margin > 0.15:
            score += 2.0
        elif margin > 0.10:
            score += 1.0
        elif margin < 0.05:
            score -= 1.0
        
        # Current Ratio (Liquidity) contribution (0-2 points)
        current = metrics.get('current_ratio', 1.0)
        if current > 2.0:
            score += 2.0
        elif current > 1.5:
            score += 1.0
        elif current < 1.0:
            score -= 2.0
        
        # Interest Coverage contribution (0-1 point)
        coverage = metrics.get('interest_coverage', 0)
        if coverage > 5.0:
            score += 1.0
        elif coverage < 2.0:
            score -= 1.0
        
        return max(0.0, min(10.0, score))
    
    def combined_risk_score(self, tech_score: float, fund_score: float, 
                           tech_weight: float = 0.6) -> Tuple[float, str]:
        """
        Combine technical and fundamental scores
        Returns: (score, grade)
        """
        combined = tech_score * tech_weight + fund_score * (1 - tech_weight)
        
        # Convert to letter grade
        if combined >= 9.0:
            grade = "A+"
        elif combined >= 8.5:
            grade = "A"
        elif combined >= 8.0:
            grade = "A-"
        elif combined >= 7.5:
            grade = "B+"
        elif combined >= 7.0:
            grade = "B"
        elif combined >= 6.5:
            grade = "B-"
        elif combined >= 6.0:
            grade = "C+"
        elif combined >= 5.5:
            grade = "C"
        elif combined >= 5.0:
            grade = "C-"
        elif combined >= 4.0:
            grade = "D"
        else:
            grade = "F"
        
        return combined, grade
    
    # ==================== WARNING GENERATION ====================
    
    def generate_warnings(self, symbol: str, tech_metrics: Dict, fund_metrics: Dict) -> List[str]:
        """Generate risk warnings based on metrics"""
        warnings = []
        
        # Technical warnings
        if tech_metrics.get('max_drawdown', 0) < -0.30:
            warnings.append(f"{symbol}: High drawdown risk (>{abs(tech_metrics['max_drawdown']*100):.1f}%)")
        
        if tech_metrics.get('volatility', 0) > 0.40:
            warnings.append(f"{symbol}: Very high volatility ({tech_metrics['volatility']*100:.1f}%)")
        
        if tech_metrics.get('beta', 1.0) > 1.5:
            warnings.append(f"{symbol}: High market sensitivity (Beta: {tech_metrics['beta']:.2f})")
        
        # Fundamental warnings
        if fund_metrics.get('debt_equity', 0) > 1.5:
            warnings.append(f"{symbol}: High leverage (D/E: {fund_metrics['debt_equity']:.2f})")
        
        if fund_metrics.get('current_ratio', 2.0) < 1.0:
            warnings.append(f"{symbol}: Liquidity concern (Current Ratio: {fund_metrics['current_ratio']:.2f})")
        
        if fund_metrics.get('roe', 0.15) < 0.05:
            warnings.append(f"{symbol}: Low profitability (ROE: {fund_metrics['roe']*100:.1f}%)")
        
        if fund_metrics.get('interest_coverage', 5.0) < 2.0:
            warnings.append(f"{symbol}: Interest coverage risk ({fund_metrics['interest_coverage']:.1f}x)")
        
        return warnings
