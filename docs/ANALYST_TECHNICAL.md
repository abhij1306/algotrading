# ANALYST MODULE - Technical Documentation

**Version:** 2.0.0  
**Last Updated:** 2025-12-22  
**Module:** Portfolio Analysis & Research

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Portfolio Construction](#portfolio-construction)
4. [Backtest Logic](#backtest-logic)
5. [Risk Analysis](#risk-analysis)
6. [Assumptions](#assumptions)
7. [Module Interactions](#module-interactions)
8. [API Reference](#api-reference)
9. [Database Schema](#database-schema)

---

## 1. Overview

### Purpose
The Analyst module provides portfolio-level analysis, backtesting, and research capabilities. It enables users to construct portfolios, test strategies with **dynamic parameters**, and analyze risk/return characteristics.

### Key Differences vs. Quant Module

| Feature | Analyst | Quant |
|---------|---------|-------|
| **Purpose** | Research & exploration | Production trading |
| **Parameters** | Dynamic (user-adjustable) | Locked (immutable) |
| **Approvals** | Not required | Governance required |
| **Focus** | Return optimization | Risk management |
| **Data** | Historical backtest | Live + paper trading |

### Key Features
- **Portfolio Builder**: Multi-stock portfolio construction
- **Dynamic Backtesting**: Adjustable parameters for research
- **Correlation Analysis**: Inter-stock relationships
- **Risk Metrics**: VaR, CVaR, correlation matrices
- **Scenario Analysis**: What-if simulations

---

## 2. Architecture

```
┌─────────────────────────────────────────────────┐
│            ANALYST MODULE                        │
├─────────────────────────────────────────────────┤
│                                                   │
│  ┌──────────────┐      ┌──────────────┐        │
│  │   Portfolio  │      │   Backtest   │        │
│  │   Builder    │─────►│    Engine    │        │
│  └──────────────┘      └──────────────┘        │
│         │                      │                 │
│         ▼                      ▼                 │
│  ┌──────────────┐      ┌──────────────┐        │
│  │  Risk        │      │  Performance │        │
│  │  Analysis    │      │  Metrics     │        │
│  └──────────────┘      └──────────────┘        │
│         │                      │                 │
│         └──────────┬───────────┘                │
│                    ▼                             │
│         ┌────────────────────┐                  │
│         │  Shared Backtest   │                  │
│         │  Core (with Quant) │                  │
│         └────────────────────┘                  │
└─────────────────────────────────────────────────┘
```

### Data Flow

```
User Creates Portfolio
    ↓
Select Stocks + Allocations
    ↓
Configure Backtest Parameters (DYNAMIC)
    ↓
Backend /api/analyst/backtest/run
    ↓
AnalystWrapper (allows param changes)
    ↓
BacktestCore.run()
    ↓
Calculate Portfolio Metrics
    ↓
Store Results + Return JSON
```

---

## 3. Portfolio Construction

### Portfolio Types

#### 1. Equal Weight
```python
# Each stock gets equal allocation
allocation = 100 / num_stocks
```

#### 2. Market Cap Weight
```python
# Allocation proportional to market cap
total_mcap = sum(stock.market_cap for stock in portfolio)
allocation[stock] = (stock.market_cap / total_mcap) * 100
```

#### 3. Custom Allocation
```python
# User-defined weights
allocations = {"RELIANCE": 30, "TCS": 25, "INFY": 20, ...}
# Must sum to 100%
```

#### 4. Risk Parity
```python
# Inverse volatility weighting
volatilities = [stock.volatility for stock in portfolio]
inv_vol = [1/v for v in volatilities]
allocation[stock] = (inv_vol[stock] / sum(inv_vol)) * 100
```

### Portfolio Database Schema

```sql
CREATE TABLE user_portfolios (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(50) DEFAULT 'default_user',
    name VARCHAR(200) NOT NULL,
    description TEXT,
    total_invested FLOAT,
    allocation_type VARCHAR(50),  -- EQUAL, MCAP, CUSTOM, RISK_PARITY
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE portfolio_positions (
    id SERIAL PRIMARY KEY,
    portfolio_id INT REFERENCES user_portfolios(id),
    company_id INT REFERENCES companies(id),
    quantity FLOAT,
    avg_buy_price FLOAT,
    invested_value FLOAT,
    allocation_pct FLOAT,
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(portfolio_id, company_id)
);
```

### Portfolio Construction Logic

**File:** `backend/app/routers/analyst.py`

```python
@router.post("/portfolios/create")
async def create_portfolio(
    name: str,
    stocks: List[dict],  # [{"symbol": "RELIANCE", "allocation": 30}, ...]
    allocation_type: str = "CUSTOM",
    total_capital: float = 1000000,
    db: Session = Depends(get_db)
):
    """
    Create new portfolio with allocations
    
    Validations:
    - Allocations sum to 100%
    - All symbols exist in database
    - No duplicate symbols
    """
    # Validate allocations sum to 100
    total_allocation = sum(s['allocation'] for s in stocks)
    if abs(total_allocation - 100.0) > 0.01:
        raise HTTPException(400, "Allocations must sum to 100%")
    
    # Create portfolio
    portfolio = UserPortfolio(
        name=name,
        allocation_type=allocation_type,
        total_invested=total_capital
    )
    db.add(portfolio)
    db.flush()
    
    # Add positions
    for stock in stocks:
        company = db.query(Company).filter_by(symbol=stock['symbol']).first()
        if not company:
            raise HTTPException(404, f"Symbol not found: {stock['symbol']}")
        
        allocated_value = total_capital * (stock['allocation'] / 100)
        
        # Get latest price
        latest_price = db.query(HistoricalPrice).filter_by(
            company_id=company.id
        ).order_by(HistoricalPrice.date.desc()).first()
        
        quantity = allocated_value / latest_price.close if latest_price else 0
        
        position = PortfolioPosition(
            portfolio_id=portfolio.id,
            company_id=company.id,
            quantity=quantity,
            avg_buy_price=latest_price.close if latest_price else 0,
            invested_value=allocated_value,
            allocation_pct=stock['allocation']
        )
        db.add(position)
    
    db.commit()
    return {"portfolio_id": portfolio.id, "name": name}
```

---

## 4. Backtest Logic

### Analyst Wrapper

**File:** `backend/app/engines/backtest/analyst_wrapper.py`

```python
class AnalystWrapper:
    """
    Analyst-specific backtest wrapper
    
    Key Differences from Quant:
    - Parameters are DYNAMIC (user can change)
    - No governance approval required
    - Results are for research, not production
    - Multiple runs allowed with different params
    """
    
    def __init__(self, db: Session):
        self.core = BacktestCore(db)
    
    async def run_portfolio_backtest(
        self,
        portfolio_id: int,
        start_date: date,
        end_date: date,
        rebalance_frequency: str = "MONTHLY",
        **strategy_params
    ) -> dict:
        """
        Run backtest for entire portfolio
        
        Args:
            portfolio_id: Database portfolio ID
            start_date, end_date: Backtest period
            rebalance_frequency: DAILY, WEEKLY, MONTHLY, NONE
            **strategy_params: Dynamic strategy parameters
        
        Returns:
            Portfolio-level metrics + per-stock results
        """
        # Get portfolio positions
        portfolio = self.db.query(UserPortfolio).get(portfolio_id)
        positions = self.db.query(PortfolioPosition).filter_by(
            portfolio_id=portfolio_id
        ).all()
        
        # Extract symbols and allocations
        symbols = [p.company.symbol for p in positions]
        allocations = {p.company.symbol: p.allocation_pct / 100 for p in positions}
        
        # Fetch OHLCV data
        data = self.core.data_provider.get_ohlcv(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date
        )
        
        # Initialize portfolio value tracking
        portfolio_equity = pd.Series(index=data.index.get_level_values('date').unique())
        portfolio_equity.iloc[0] = portfolio.total_invested
        
        # Track per-stock performance
        stock_returns = {}
        
        for symbol in symbols:
            stock_data = data.xs(symbol, level='symbol')
            stock_allocation = allocations[symbol]
            
            # Calculate stock returns
            stock_pnl = stock_data['close'].pct_change().fillna(0)
            stock_equity = (1 + stock_pnl).cumprod() * portfolio.total_invested * stock_allocation
            
            stock_returns[symbol] = stock_equity
        
        # Combine into portfolio
        for date in portfolio_equity.index:
            portfolio_equity[date] = sum(
                stock_returns[symbol].get(date, 0) for symbol in symbols
            )
        
        # Calculate metrics
        metrics = self._calculate_portfolio_metrics(portfolio_equity, allocations)
        
        # Store results
        run_id = self._store_results(portfolio_id, metrics, portfolio_equity)
        
        return {
            "run_id": run_id,
            "metrics": metrics,
            "equity_curve": portfolio_equity.to_dict(),
            "per_stock": {
                symbol: self._calculate_stock_metrics(stock_returns[symbol])
                for symbol in symbols
            }
        }
```

### Rebalancing Logic

```python
def _rebalance_portfolio(
    self,
    current_allocations: dict,
    target_allocations: dict,
    current_values: dict
) -> dict:
    """
    Calculate trades needed to rebalance portfolio
    
    Args:
        current_allocations: {symbol: current_pct}
        target_allocations: {symbol: target_pct}
        current_values: {symbol: current_value}
    
    Returns:
        {symbol: trade_value}  # Positive = buy, Negative = sell
    """
    total_value = sum(current_values.values())
    
    trades = {}
    for symbol in target_allocations:
        target_value = total_value * target_allocations[symbol]
        current_value = current_values.get(symbol, 0)
        trade_value = target_value - current_value
        
        if abs(trade_value) > total_value * 0.01:  # >1% drift
            trades[symbol] = trade_value
    
    return trades
```

---

## 5. Risk Analysis

### Correlation Analysis

```python
def calculate_correlation_matrix(returns: DataFrame) -> DataFrame:
    """
    Calculate rolling correlation between portfolio stocks
    
    Args:
        returns: DataFrame with columns = symbols, index = dates
    
    Returns:
        Correlation matrix
    """
    # Pearson correlation
    corr_matrix = returns.corr()
    
    return corr_matrix

def identify_clusters(corr_matrix: DataFrame, threshold: float = 0.7):
    """
    Identify highly correlated stock clusters
    
    High correlation = diversification failure
    """
    clusters = []
    for i, symbol1 in enumerate(corr_matrix.columns):
        cluster = [symbol1]
        for j, symbol2 in enumerate(corr_matrix.columns[i+1:], i+1):
            if corr_matrix.iloc[i, j] > threshold:
                cluster.append(symbol2)
        if len(cluster) > 1:
            clusters.append(cluster)
    
    return clusters
```

### Value at Risk (VaR)

```python
def calculate_var(
    returns: Series,
    confidence_level: float = 0.95,
    method: str = "historical"
) -> float:
    """
    Calculate Value at Risk
    
    Methods:
    - historical: Use empirical distribution
    - parametric: Assume normal distribution
    - monte_carlo: Simulate future paths
    
    Returns:
        VaR as percentage (e.g., -2.5 = 2.5% loss)
    """
    if method == "historical":
        # Sort returns and take percentile
        var = returns.quantile(1 - confidence_level)
    
    elif method == "parametric":
        # Assume normal distribution
        mean = returns.mean()
        std = returns.std()
        z_score = norm.ppf(1 - confidence_level)
        var = mean + z_score * std
    
    elif method == "monte_carlo":
        # Simulate 10,000 paths
        n_simulations = 10000
        simulated_returns = np.random.normal(
            returns.mean(),
            returns.std(),
            n_simulations
        )
        var = np.percentile(simulated_returns, (1 - confidence_level) * 100)
    
    return var * 100  # Convert to percentage
```

### Conditional VaR (CVaR / Expected Shortfall)

```python
def calculate_cvar(returns: Series, confidence_level: float = 0.95) -> float:
    """
    Calculate average loss beyond VaR threshold
    
    CVaR is more conservative than VaR (measures tail risk)
    """
    var = calculate_var(returns, confidence_level)
    
    # Average of returns worse than VaR
    tail_returns = returns[returns <= var]
    cvar = tail_returns.mean()
    
    return cvar * 100
```

### Risk-Adjusted Metrics

```python
def calculate_risk_metrics(equity_curve: Series) -> dict:
    """
    Calculate comprehensive risk metrics
    
    Returns:
        - Sharpe Ratio
        - Sortino Ratio
        - Calmar Ratio
        - Max Drawdown
        - VaR (95%, 99%)
        - CVaR
    """
    returns = equity_curve.pct_change().dropna()
    
    # Sharpe Ratio (annualized)
    sharpe = (returns.mean() * 252) / (returns.std() * np.sqrt(252))
    
    # Sortino Ratio (downside deviation only)
    downside_returns = returns[returns < 0]
    sortino = (returns.mean() * 252) / (downside_returns.std() * np.sqrt(252))
    
    # Calmar Ratio (return / max drawdown)
    annual_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) ** (252 / len(equity_curve)) - 1
    max_dd = calculate_max_drawdown(equity_curve)
    calmar = annual_return / abs(max_dd) if max_dd != 0 else 0
    
    # VaR
    var_95 = calculate_var(returns, 0.95)
    var_99 = calculate_var(returns, 0.99)
    
    # CVaR
    cvar_95 = calculate_cvar(returns, 0.95)
    
    return {
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "max_drawdown": max_dd,
        "var_95": var_95,
        "var_99": var_99,
        "cvar_95": cvar_95
    }
```

---

## 6. Assumptions

### Portfolio Construction Assumptions
1. **Fractional Shares:** Allowed (not real-world for all brokers)
2. **No Minimum Investment:** Can buy any quantity
3. **Instant Execution:** All orders fill at close price
4. **No Liquidity Constraints:** Can buy/sell any amount
5. **Rebalancing Costs:** Flat ₹20 per trade

### Backtest Assumptions
1. **Corporate Actions:** Pre-adjusted prices
2. **Dividends:** Not reinvested (cash)
3. **Taxes:** Not modeled
4. **Margin:** Cash-only portfolio (no leverage)
5. **Survivorship Bias:** Present (uses current universe)

### Risk Assumptions
1. **Normal Distribution:** Parametric VaR assumes normality (not always true)
2. **Independence:** Returns assumed independent (autocorrelation ignored)
3. **Stationarity:** Historical std dev assumed stable
4. **No Black Swans:** VaR doesn't capture extreme tail events well

---

## 7. Module Interactions

### Analyst ↔ Screener
```
Analyst reads from Screener:
    ├─ Stock universe (filtered by index)
    ├─ Technical scores for stock selection
    └─ Latest prices for valuation

Screener benefits from Analyst:
    ├─ Correlation data (diversification checks)
    └─ Portfolio performance benchmarks
```

### Analyst ↔ Quant
```
Shared Components:
    ├─ BacktestCore (same engine)
    ├─ DataProvider (unified data access)
    └─ Risk calculation functions

Key Difference:
    ├─ Analyst: Dynamic params, research-focused
    └─ Quant: Locked params, production-focused
```

### Analyst ↔ Market Data
```
Analyst depends on Market Data for:
    ├─ Historical OHLCV
    ├─ Corporate action adjustments
    ├─ Latest prices for portfolio valuation
    └─ Indicator values
```

---

## 8. API Reference

### Portfolio Management

#### POST /api/analyst/portfolios/create
Create new portfolio

**Request:**
```json
{
  "name": "Tech Portfolio",
  "stocks": [
    {"symbol": "TCS", "allocation": 40},
    {"symbol": "INFY", "allocation": 35},
    {"symbol": "WIPRO", "allocation": 25}
  ],
  "total_capital": 1000000
}
```

#### GET /api/analyst/portfolios
List user portfolios

#### GET /api/analyst/portfolios/{id}
Get portfolio details including current valuations

### Backtesting

#### POST /api/analyst/backtest/run
Run portfolio backtest

**Request:**
```json
{
  "portfolio_id": 123,
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "rebalance_frequency": "MONTHLY"
}
```

**Response:**
```json
{
  "run_id": "analyst_bt_20241222_120000",
  "metrics": {
    "total_return": 18.5,
    "sharpe_ratio": 1.4,
    "max_drawdown": -8.2,
    "var_95": -1.8,
    "cvar_95": -2.5
  },
  "per_stock": {
    "TCS": {"return": 22.1, "contribution": 8.8},
    "INFY": {"return": 15.3, "contribution": 5.4}
  }
}
```

---

## 9. Database Schema

### user_portfolios
```sql
CREATE TABLE user_portfolios (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(50),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    total_invested FLOAT,
    allocation_type VARCHAR(50),
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

### portfolio_positions
```sql
CREATE TABLE portfolio_positions (
    id SERIAL PRIMARY KEY,
    portfolio_id INT REFERENCES user_portfolios(id),
    company_id INT REFERENCES companies(id),
    quantity FLOAT,
    avg_buy_price FLOAT,
    invested_value FLOAT,
    allocation_pct FLOAT,
    UNIQUE(portfolio_id, company_id)
);
```

### computed_risk_metrics
```sql
CREATE TABLE computed_risk_metrics (
    id SERIAL PRIMARY KEY,
    portfolio_id INT REFERENCES user_portfolios(id),
    metric_name VARCHAR(100),
    metric_value FLOAT,
    computed_at TIMESTAMP
);
```

---

## Conclusion

The Analyst module provides **flexible, research-oriented portfolio analysis** with dynamic parameter adjustment, comprehensive risk metrics, and correlation analysis. It shares the backtest engine with Quant but maintains a research-first, exploration-friendly approach.

**Key Principles:**
- Dynamic parameters for exploration
- Portfolio-level analysis
- Risk-adjusted performance
- No production constraints
- Correlation-aware construction
