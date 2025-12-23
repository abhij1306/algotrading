# QUANT MODULE - Technical Documentation

**Version:** 3.0.0 (SmartTrader)  
**Last Updated:** 2025-12-22  
**Module:** Quantitative Strategy Research & Governance

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Strategy Lifecycle](#strategy-lifecycle)
4. [Backtest Engine](#backtest-engine)
5. [Paper Trading System](#paper-trading-system)
6. [Data Pipeline](#data-pipeline)
7. [Assumptions & Constraints](#assumptions--constraints)
8. [Module Interactions](#module-interactions)
9. [API Reference](#api-reference)
10. [Database Schema](#database-schema)

---

# Quant Module Technical Specification (Phase 2)

## Overview
The Quant Module transforms the platform from a "Scanner" to a "Fund Manager". It introduces a **Risk-First** architecture where Portfolio Governance dictates Strategy Allocation.

## Core Philosophy
- **Strategies are Bricks**: Immutable logic blocks (e.g., Trend, MeanRev).
- **Policy is Mortar**: Strict rules defined by the user (Risk Limits).
- **Governance Layer**: The `PortfolioBacktestCore` engine enforcing limits.

## 1. Database Schema (New)
### `portfolio_policies`
- Defines the "Rules of Engagement".
- **Risk Limits**: `cash_reserve_percent`, `daily_stop_loss_percent`, `max_equity_exposure_percent`.
- **Allocator Logic**: `allocation_sensitivity` (Low/Med/High), `correlation_penalty`.

### `research_portfolios`
- Immutable composition of strategies.
- Links `policy_id` to a list of `{strategy_id, allocation_percent}`.

## 2. Risk Engine (`PortfolioBacktestCore`)
The engine runs an event-driven simulation focusing on risk states rather than just alpha.

### State Machine
1. **NORMAL**: Full exposure allowed (up to Policy Limit).
2. **CAUTIOUS**: Exposure cut by 50% (or stricter based on Sensitivity). Triggered by >10% Drawdown.
3. **DEFENSIVE**: Minimum exposure (10% or 0%). Triggered by >20% Drawdown.
4. **HALTED**: Trading stopped.

### Governance Logic
- **Cash Reserve**: `Active Capital = Equity * (1 - CashReserve%)`.
- **Strategy Cap**: Individual strategy weight clipped at `max_strategy_allocation_percent`.
- **Daily Stop Loss**: If daily loss hits limit (e.g. 2%), position is assumed flattened at that loss level, and State transitions to CAUTIOUS.

## 3. Architecture
- **Frontend**: `PolicyEditor` (Risk Inputs), `StrategyAllocation` (Correlation Matrix).
- **Backend**: `portfolio_research.py` (Endpoints), `PortfolioBacktestCore` (Engine).
- **Data**: Uses `historical_prices` (Screening) and `backtest_daily_results` (Strategy Returns).

## 1. Overview

### Purpose
The Quant module implements **SmartTrader 3.0** - a production-grade quantitative trading system based on the philosophy: **"Modular Independence, Shared Backbone."** It manages the complete lifecycle of systematic trading strategies from research to live execution.

### Core Philosophy (MASTER_PROMPT)

**Non-Negotiable Principles:**
1. **Research explains risk** — not returns
2. **Governance controls permission** — not performance
3. **Monitoring observes** — never configures
4. **No UI element without database backing**
5. **No mock, placeholder, or synthetic data**

### Key Features
- **Strategy Contracts**: Immutable strategy definitions
- **Lifecycle Management**: RESEARCH → PAPER → LIVE → RETIRED
- **Portfolio Research**: Multi-strategy backtesting
- **Paper Trading**: Automated validation system
- **Risk Forensics**: Drawdown analysis, not return optimization

### Technology Stack
- **Backend**: Python 3.9+, FastAPI
- **Backtest Engine**: Pandas, NumPy
- **Database**: PostgreSQL (source of truth)
- **Scheduler**: APScheduler (paper trading automation)
- **Frontend**: Next.js (React)

---

## 2. Architecture

### Module Structure

```
SmartTrader 3.0 (Quant Module)
│
├── Research
│   ├── Strategy Research (Individual strategy analysis)
│   └── Port

folio Research (Multi-strategy combinations)
│
├── Governance
│   ├── Strategies (Lifecycle management)
│   ├── Universes (Asset definitions)
│   └── Portfolio Policy (System-level risk controls)
│
├── Monitoring (DEFAULT LANDING - Read-only)
│   ├── Live Positions
│   ├── PnL Tracking
│   └── Drawdown Alerts
│
└── Settings
    ├── API Configuration
    └── System Parameters
```

### Component Diagram

```
┌───────────────────────────────────────────────────────────┐
│                   QUANT MODULE (v3.0)                      │
├───────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐      ┌──────────────┐    ┌───────────┐ │
│  │   Research   │      │  Governance  │    │ Monitoring│ │
│  │   (Analyst)  │─────►│  (Approvals) │───►│ (Display) │ │
│  └──────────────┘      └──────────────┘    └───────────┘ │
│         │                      │                   │       │
│         ▼                      ▼                   ▼       │
│  ┌─────────────────────────────────────────────────────┐ │
│  │          DATA & EXECUTION BACKBONE                   │ │
│  ├─────────────────────────────────────────────────────┤ │
│  │  • StrategyContract (governance)                    │ │
│  │  • BacktestRun (immutable results)                  │ │
│  │  • PaperTradingService (validation)                 │ │
│  │  • PostgreSQL (source of truth)                     │ │
│  └─────────────────────────────────────────────────────┘ │
│         │                      │                   │       │
│         ▼                      ▼                   ▼       │
│  ┌──────────┐        ┌──────────────┐    ┌─────────────┐│
│  │ Backtest │        │    Paper     │    │    Live     ││
│  │  Engine  │        │   Trading    │    │   Broker    ││
│  └──────────┘        └──────────────┘    └─────────────┘│
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## 3. Strategy Lifecycle

### Lifecycle States

```
┌─────────────┐
│  RESEARCH   │  ← New strategies start here
└──────┬──────┘
       │ Approve (after backtest validation)
       ▼
┌─────────────┐
│    PAPER    │  ← Live validation with real data
└──────┬──────┘
       │ Graduate (after 3+ months success)
       ▼
┌─────────────┐
│    LIVE     │  ← Production trading
└──────┬──────┘
       │ Retire (performance degradation)
       ▼
┌─────────────┐
│   RETIRED   │  ← Archived, no longer active
└─────────────┘
```

### Transition Rules

#### RESEARCH → PAPER
**Requirements:**
- Backtest shows max DD < 15%
- Sharpe ratio > 1.0
- Win rate > 45%
- "WHEN IT LOSES" is documented
- Approved by governance

**Code:** `POST /api/quant/lifecycle/transition`
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "new_state": "PAPER",
  "reason": "Passed backtest validation, max DD 12.3%",
  "approved_by": "admin"
}
```

#### PAPER → LIVE
**Requirements:**
- 90 days minimum in PAPER
- Real-world DD within 120% of backtest DD
- No consecutive losing days > 5
- Manual approval required

#### LIVE → RETIRED
**Emergency Stop:**
- DD exceeds 150% of backtest max
- 10 consecutive losing days
- Correlation breakdown
- Manual emergency stop

### Lifecycle Tracking

**Database:** `strategy_contracts` table
```sql
CREATE TABLE strategy_contracts (
    strategy_id VARCHAR(100) PRIMARY KEY,
    lifecycle_state VARCHAR(20) DEFAULT 'RESEARCH',
    state_since TIMESTAMP DEFAULT NOW(),
    approved_at TIMESTAMP,
    approved_by VARCHAR(50),
    when_loses TEXT NOT NULL  -- MANDATORY
);
```

**Audit Trail:** Every transition logged in `allocator_decisions` table

---

## 4. Backtest Engine

### Architecture

```
Backtest Request
    ↓
[Strategy Params] + [Universe] + [Date Range]
    ↓
BacktestCore.run()
    ↓
┌──────────────────────────┐
│  1. Data Validation      │ ← Ensure sufficient data
│  2. Indicator Calculation│ ← Pre-compute signals
│  3. Signal Generation    │ ← Apply strategy logic
│  4. Position Sizing      │ ← Calculate quantities
│  5. Execution Simulation │ ← Fill prices, slippage
│  6. PnL Calculation      │ ← Mark-to-market
│  7. Risk Metrics         │ ← DD, Sharpe, etc.
└──────────────────────────┘
    ↓
Store in backtest_runs (IMMUTABLE)
    ↓
Return JSON response
```

### Core Components

#### File: `backend/app/engines/backtest/core.py`

```python
class BacktestCore:
    """
    Shared backtest engine for both Analyst and Quant modules
    
    Key Principles:
    - Immutable results (once stored, never recomputed)
    - Database-first (all data from PostgreSQL)
    - No look-ahead bias
    - Realistic execution (slippage, commissions)
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.data_provider = DataProvider(db)
    
    def run(self, config: BacktestConfig) → BacktestResult:
        """
        Main backtest execution
        
        Args:
            config: {
                strategy_id, universe, start_date, end_date,
                capital, params, execution_mode
            }
        
        Returns:
            BacktestResult with equity curve, metrics, trades
        """
        # 1. Validate configuration
        self._validate_config(config)
        
        # 2. Load data
        prices = self.data_provider.get_ohlcv(
            symbols=config.universe,
            start=config.start_date,
            end=config.end_date
        )
        
        # 3. Calculate indicators
        indicators = self._calculate_indicators(prices, config.params)
        
        # 4. Generate signals
        signals = self._generate_signals(indicators, config.strategy_id)
        
        # 5. Simulate execution
        trades = self._execute_trades(signals, prices, config.capital)
        
        # 6. Calculate metrics
        metrics = self._calculate_metrics(trades)
        
        # 7. Store results (IMMUTABLE)
        run_id = self._store_results(config, metrics, trades)
        
        return BacktestResult(
            run_id=run_id,
            metrics=metrics,
            equity_curve=self._build_equity_curve(trades),
            trades=trades
        )
```

### Strategy Logic Implementation

#### Example: ORB (Opening Range Breakout)

```python
def ORB_NIFTY_5MIN(df: DataFrame, params: dict) → Series:
    """
    Opening Range Breakout Strategy
    
    Logic:
    1. Define opening range (9:15-9:30 AM)
    2. Calculate breakout levels
    3. Enter on breakout with confirmation
    4. Exit on target or EOD
    
    Params:
        or_duration_minutes: 15 (default)
        breakout_threshold: 0.001 (0.1%)
        stop_loss_pct: 1.0
        take_profit_pct: 2.0
    
    WHEN IT LOSES:
    "Low volatility days with no clear directional bias"
    """
    signals = pd.Series(0, index=df.index)
    
    # Filter market hours
    df = df.between_time('09:15', '15:30')
    
    # Group by date
    for date, day_data in df.groupby(df.index.date):
        # Opening range (first N minutes)
        or_end = day_data.index[0] + timedelta(minutes=params['or_duration_minutes'])
        or_data = day_data[day_data.index <= or_end]
        
        or_high = or_data['high'].max()
        or_low = or_data['low'].min()
        or_range = or_high - or_low
        
        # Breakout levels
        buy_level = or_high + (or_range * params['breakout_threshold'])
        sell_level = or_low - (or_range * params['breakout_threshold'])
        
        # Post-OR data
        post_or = day_data[day_data.index > or_end]
        
        for idx, row in post_or.iterrows():
            # Long breakout
            if row['high'] > buy_level and signals.loc[:idx].sum() == 0:
                signals.loc[idx] = 1  # BUY
                entry_price = buy_level
                stop_loss = entry_price * (1 - params['stop_loss_pct']/100)
                take_profit = entry_price * (1 + params['take_profit_pct']/100)
            
            # Short breakout
            elif row['low'] < sell_level and signals.loc[:idx].sum() == 0:
                signals.loc[idx] = -1  # SELL
                entry_price = sell_level
                stop_loss = entry_price * (1 + params['stop_loss_pct']/100)
                take_profit = entry_price * (1 - params['take_profit_pct']/100)
            
            # Exit logic
            if signals.loc[:idx].sum() != 0:
                if signals.loc[:idx].sum() > 0:  # Long position
                    if row['low'] <= stop_loss or row['high'] >= take_profit:
                        signals.loc[idx] = -1  # EXIT
                else:  # Short position
                    if row['high'] >= stop_loss or row['low'] <= take_profit:
                        signals.loc[idx] = 1  # EXIT
        
        # EOD exit
        if signals.sum() != 0:
            signals.iloc[-1] = -signals.sum()
    
    return signals
```

### Backtest Assumptions

#### Execution Model
1. **Fill Price:** Next bar's open (no look-ahead)
2. **Slippage:** 0.05% for liquid stocks
3. **Commission:** ₹20 per order (flat)
4. **Market Impact:** Ignored (assumes small size)
5. **Partial Fills:** Not modeled (all-or-nothing)

#### Data Assumptions
1. **Survivorship Bias:** NOT corrected (uses current universe)
2. **Corporate Actions:** Adjusted prices used
3. **Splits/Bonuses:** Historical data pre-adjusted
4. **Data Quality:** Assumes clean, gapless data
5. **Liquidity:** Assumes all orders fillable

#### Risk Calculations
```python
def calculate_risk_metrics(equity_curve: Series) → dict:
    """
    Calculates drawdown-centric risk metrics
    
    Returns:
        - max_drawdown: Maximum peak-to-trough decline
        - max_dd_duration: Longest DD period (days)
        - recovery_time: Median time to recover from DD
        - percentile_95_dd: 95th percentile DD
        - sharpe_ratio: (mean_return - rf) / std_return
        - sortino_ratio: Uses downside deviation only
    """
    returns = equity_curve.pct_change().dropna()
    
    # Drawdown series
    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max * 100
    
    # Max DD
    max_dd = drawdown.min()
    
    # DD duration
    underwater = drawdown < 0
    dd_periods = underwater.astype(int).groupby(
        (underwater != underwater.shift()).cumsum()
    ).sum()
    max_dd_duration = dd_periods.max()
    
    # Recovery time (when DD returns to 0)
    recovery_times = []
    in_dd = False
    dd_start = None
    for i, dd in drawdown.items():
        if dd < 0 and not in_dd:
            dd_start = i
            in_dd = True
        elif dd >= 0 and in_dd:
            recovery_times.append((i - dd_start).days)
            in_dd = False
    
    # 95th percentile DD
    percentile_95_dd = drawdown.quantile(0.05)  # 5th percentile (worst)
    
    # Sharpe (annualized)
    sharpe = (returns.mean() * 252) / (returns.std() * np.sqrt(252))
    
    # Sortino (only downside)
    downside_returns = returns[returns < 0]
    sortino = (returns.mean() * 252) / (downside_returns.std() * np.sqrt(252))
    
    return {
        'max_drawdown': max_dd,
        'max_dd_duration': max_dd_duration,
        'recovery_time_median': np.median(recovery_times) if recovery_times else 0,
        'percentile_95_dd': percentile_95_dd,
        'sharpe_ratio': sharpe,
        'sortino_ratio': sortino
    }
```

---

## 5. Paper Trading System

### Architecture

```
┌────────────────────────────────────────────┐
│       Paper Trading Scheduler               │
│   (runs_paper_trading_scheduler.py)        │
└────────────────┬───────────────────────────┘
                 │
                 ▼
    ┌────────────────────────┐
    │  Market Hours Check    │ ← 9:15-15:30, Mon-Fri
    └────────┬───────────────┘
             │ ✓ (during market hours)
             ▼
    ┌────────────────────────┐
    │  Fetch LIVE Strategies │ ← lifecycle_state = 'PAPER' or 'LIVE'
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Generate Signals      │ ← Apply strategy logic to live data
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Create Orders         │ ← INSERT INTO paper_orders
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Simulate Execution    │ ← Fill at next tick
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Update Positions      │ ← UPDATE paper_positions
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Calculate PnL         │ ← Mark-to-market
    └────────┬───────────────┘
             │
             ▼
    ┌────────────────────────┐
    │  Check Risk Limits     │ ← Daily stop loss, max DD
    └────────┬───────────────┘
             │ If breached
             ▼
    ┌────────────────────────┐
    │  Emergency Stop        │ ← Close all, mark RETIRED
    └────────────────────────┘
```

### Scheduler Configuration

**File:** `backend/run_paper_trading_scheduler.py`

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

IST = pytz.timezone('Asia/Kolkata')

scheduler = AsyncIOScheduler(timezone=IST)

# Run every minute during market hours
scheduler.add_job(
    paper_trading_cycle,
    trigger=CronTrigger(
        minute='*/1',      # Every minute
        hour='9-15',       # 9 AM - 3 PM
        day_of_week='mon-fri',
        timezone=IST
    ),
    id='paper_trading',
    name='Paper Trading Cycle'
)

scheduler.start()
```

### Paper Trading Logic

**File:** `backend/app/services/paper_trading_service.py`

```python
class PaperTradingService:
    """
    Manages paper trading execution
    
    Key Features:
    - Real-time signal generation
    - Simulated order execution
    - Position tracking
    - Risk monitoring
    - Performance logging
    """
    
    async def run_cycle(self):
        """
        Executed every minute during market hours
        
        Steps:
        1. Get active strategies (PAPER + LIVE)
        2. Fetch latest market data
        3. Generate signals
        4. Create/execute orders
        5. Update positions
        6. Calculate PnL
        7. Check risk limits
        """
        # 1. Get active strategies
        strategies = self.db.query(StrategyContract).filter(
            StrategyContract.lifecycle_state.in_(['PAPER', 'LIVE'])
        ).all()
        
        for strategy in strategies:
            # 2. Get latest data
            latest_data = await self.data_provider.get_latest(
                symbols=strategy.allowed_universes
            )
            
            # 3. Generate signal
            signal = self._apply_strategy_logic(
                strategy.strategy_id, latest_data, strategy.parameters
            )
            
            if signal:
                # 4. Create order
                order = PaperOrder(
                    strategy_id=strategy.strategy_id,
                    symbol=signal['symbol'],
                    side=signal['side'],  # BUY/SELL
                    quantity=signal['quantity'],
                    order_type='MARKET',
                    status='PENDING'
                )
                self.db.add(order)
                self.db.commit()
                
                # 5. Execute immediately (paper = instant fill)
                fill_price = latest_data[signal['symbol']]['ltp']
                self._execute_order(order, fill_price)
        
        # 6. Update all positions with latest prices
        self._mark_to_market()
        
        # 7. Check risk limits
        self._check_risk_limits()
    
    def _execute_order(self, order: PaperOrder, price: float):
        """Simulates order execution"""
        # Create trade record
        trade = PaperTrade(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            value=price * order.quantity,
            commission=20.0  # Flat ₹20
        )
        self.db.add(trade)
        
        # Update/create position
        position = self.db.query(PaperPosition).filter_by(
            symbol=order.symbol
        ).first()
        
        if not position:
            position = PaperPosition(
                symbol=order.symbol,
                side='LONG' if order.side == 'BUY' else 'SHORT',
                quantity=order.quantity,
                average_price=price
            )
            self.db.add(position)
        else:
            # Update existing position
            if order.side == 'BUY':
                total_value = (position.average_price * position.quantity) + (price * order.quantity)
                position.quantity += order.quantity
                position.average_price = total_value / position.quantity
            else:  # SELL
                position.quantity -= order.quantity
                if position.quantity == 0:
                    self.db.delete(position)
        
        order.status = 'FILLED'
        self.db.commit()
```

### Risk Monitoring

```python
def _check_risk_limits(self):
    """
    Monitors portfolio-level risk limits
    
    Checks:
    1. Daily stop loss (2% max)
    2. Max drawdown (15% max)
    3. Consecutive losing days (5 max)
    4. Strategy-level allocation (25% max per strategy)
    """
    # Get today's PnL
    today_pnl_pct = self._calculate_daily_pnl()
    
    # Check daily stop loss
    if today_pnl_pct < -2.0:
        logger.critical(f"Daily stop loss breached: {today_pnl_pct}%")
        self._emergency_stop_all()
        return
    
    # Check max drawdown
    current_dd = self._calculate_current_drawdown()
    if current_dd < -15.0:
        logger.critical(f"Max drawdown breached: {current_dd}%")
        self._emergency_stop_all()
        return
    
    # Check consecutive losing days
    losing_streak = self._get_losing_streak()
    if losing_streak >= 5:
        logger.warning(f"5 consecutive losing days - review required")
        # Don't stop, but alert

def _emergency_stop_all(self):
    """Emergency stop - close all positions"""
    positions = self.db.query(PaperPosition).all()
    
    for position in positions:
        # Create market order to close
        close_order = PaperOrder(
            symbol=position.symbol,
            side='SELL' if position.side == 'LONG' else 'BUY',
            quantity=position.quantity,
            order_type='MARKET',
           status='EMERGENCY_CLOSE'
        )
        self.db.add(close_order)
        
        # Execute immediately
        latest_price = self.data_provider.get_ltp(position.symbol)
        self._execute_order(close_order, latest_price)
    
    # Mark all strategies as RETIRED
    self.db.query(StrategyContract).filter(
        StrategyContract.lifecycle_state == 'PAPER'
    ).update({'lifecycle_state': 'RETIRED'})
    
    self.db.commit()
    logger.critical("Emergency stop executed - all positions closed")
```

---

## 6. Data Pipeline

### Data Flow

```
External Sources
    ├─ NSE Bhavcopy (EOD data)
    ├─ Fyers API (Live quotes)
    └─ Manual Uploads (Corporate actions)
            ↓
Data Ingestion Layer
    ├─ update_bhavcopy.py (daily 3:45 PM)
    ├─ data_fetcher.py (on-demand)
    └─ Validation & Cleaning
            ↓
PostgreSQL (Source of Truth)
    ├─ historical_prices (OHLCV + indicators)
    ├─ companies (master data)
    └─ strategy_contracts (governance)
            ↓
Data Provider (Abstraction Layer)
    ├─ get_ohlcv(symbols, dates)
    ├─ get_latest(symbols)
    └─ Error handling
            ↓
Backtest Engine / Paper Trading
    ├─ Reads data via DataProvider
    ├─ Never writes to historical data
    └─ Stores results separately
```

### Data Provider API

**File:** `backend/app/data_layer/data_provider.py`

```python
class DataProvider:
    """
    Unified data access layer for all modules
    
    Principles:
    - Database first (PostgreSQL is source of truth)
    - Consistent error handling
    - No direct DB access outside this layer
    - Caching where appropriate
    """
    
    def get_ohlcv(
        self,
        symbols: List[str],
        start_date: date,
        end_date: date,
        indicators: List[str] = None
    ) → DataFrame:
        """
        Fetch OHLCV data with optional indicators
        
        Args:
            symbols: List of stock symbols
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            indicators: Optional list of indicator columns
        
        Returns:
            MultiIndex DataFrame (symbol, date)
        
        Raises:
            DataNotFoundError: If symbols don't exist
            InsufficientDataError: If date range unavailable
        """
        # Validate symbols exist
        valid_symbols = self.db.query(Company.symbol).filter(
            Company.symbol.in_(symbols)
        ).all()
        
        if len(valid_symbols) != len(symbols):
            missing = set(symbols) - set([s[0] for s in valid_symbols])
            raise DataNotFoundError(f"Symbols not found: {missing}")
        
        # Build query
        query = self.db.query(HistoricalPrice).join(Company).filter(
            Company.symbol.in_(symbols),
            HistoricalPrice.date.between(start_date, end_date)
        )
        
        # Select columns
        columns = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume']
        if indicators:
            columns.extend(indicators)
        
        # Execute and convert to DataFrame
        data = pd.read_sql(query.statement, self.db.bind)
        
        if data.empty:
            raise InsufficientDataError(
                f"No data for {symbols} between {start_date} and {end_date}"
            )
        
        # Set multi-index
        data = data.set_index(['symbol', 'date'])
        
        return data[columns]
    
    def get_latest(self, symbols: List[str]) → Dict:
        """
        Get latest available data for symbols
        
        Returns: {symbol: {close, volume, timestamp, ...}}
        """
        # Try Fyers API first (live)
        if self._is_market_open():
            live_data = fetch_fyers_quotes(symbols)
            if live_data:
                return live_data
        
        # Fallback to latest DB data
        latest = self.db.query(HistoricalPrice).join(Company).filter(
            Company.symbol.in_(symbols)
        ).order_by(HistoricalPrice.date.desc()).limit(len(symbols)).all()
        
        return {
            row.company.symbol: {
                'close': row.close,
                'volume': row.volume,
                'timestamp': row.date,
                'source': 'database'
            }
            for row in latest
        }
```

---

## 7. Assumptions & Constraints

### Strategy Design Assumptions

#### 1. Universe Constraints
- **NIFTY50_CORE**: Always available, liquid
- **BANKNIFTY**: Banking sector only
- **NIFTY200**: For trend filters, not all  strategies
- **Custom Universes**: Must be defined in `stock_universes` table

#### 2. Timeframe Assumptions
- **Intraday (5MIN, 15MIN)**: Positions close EOD (3:30 PM)
- **Daily (1D)**: Can hold multi-day
- **No overnight for intraday**: Risk management rule

#### 3. Parameter Immutability
Once a strategy moves to **LIVE**, parameters are **locked**. No tuning allowed.

**Rationale:** Prevents overfitting to recent data.

#### 4. "WHEN IT LOSES" Requirement
Every strategy MUST document failure modes. This is non-negotiable.

**Examples:**
- ORB: "Low volatility days with no directional bias"
- Mean Reversion: "Strong trend days and breakout regimes"
- Trend Following: "Choppy, range-bound markets"

### Backtest Constraints

#### 1. Immutable Results
Once a backtest is stored with `run_id`, it is **never recomputed**.

**Why?**
- Prevents cherry-picking
- Ensures reproducibility
- Tracks strategy evolution

#### 2. No Look-Ahead Bias
- Signals generated only on available data at bar close
- Indicators calculated with past data only
- No peeking at future prices

#### 3. Transaction Costs
- **Commission**: ₹20 per order (flat)
- **Slippage**: 0.05% (5 bps)
- **STT**: Included in slippage estimate
- **Impact**: Not modeled (assumes small size)

### Paper Trading Constraints

#### 1. Execution Model
- **Instant Fill**: Orders fill at LTP (no queue simulation)
- **No Partial Fill**: All-or-nothing
- **No Rejection**: Assumes all orders accepted
- **No Gap Risk**: Overnight gaps not modeled for intraday

#### 2. Capital Allocation
- **Per-Strategy Limit**: 25% of total capital
- **Cash Reserve**: 20% minimum
- **Max Exposure**: 80% of capital

#### 3. Risk Limits (Non-Negotiable)
- Daily stop loss: 2%
- Max portfolio DD: 15%
- Max consecutive  losing days: 5 (warning, not stop)

---

## 8. Module Interactions

### 8.1 Quant ↔ Screener
```
Quant → Screener:
    ├─ Fetch universe symbols (NIFTY50, BANKNIFTY)
    ├─ Get latest indicator values
    └─ Retrieve historical OHLCV

Screener → Quant:
    ├─ Provides technical score
    ├─ Volume shock alerts
    └─ Breakout signals (potential strategy trigger)
```

### 8.2 Quant ↔ Analyst
```
Quant → Analyst:
    ├─ Share backtest engine (BacktestCore)
    ├─ Provide risk metrics calculation
    └─ Unified data provider

Analyst → Quant:
    ├─ Portfolio construction logic
    ├─ Correlation analysis
    └─ Research findings feed strategy design
```

### 8.3 Quant ↔ Market Data
```
Quant → Market Data:
    ├─ Requests OHLCV data
    ├─ Subscribes to live quotes
    └─ Fetches indicator values

Market Data → Quant:
    ├─ Provides cleaned, adjusted prices
    ├─ Delivers real-time updates
    └─ Notifies data quality issues
```

### 8.4 Quant → Broker (Future)
```
Quant (LIVE strategies) → Broker API:
    ├─ Place orders (BUY/SELL)
    ├─ Modify/cancel orders
    ├─ Query positions
    ├─ Get account balance
    └─ Receive execution reports

Currently: Paper trading only (no broker integration)
```

---

## 9. API Reference

### Strategy Lifecycle

#### GET /api/quant/backtest/strategies
List all strategy contracts

**Response:**
```json
{
  "strategies": [
    {
      "strategy_id": "ORB_NIFTY_5MIN",
      "description": "Opening Range Breakout",
      "lifecycle_state": "LIVE",
      "regime": "Momentum",
      "when_loses": "Low volatility days...",
      "allowed_universes": ["NIFTY50_CORE"]
    }
  ]
}
```

#### POST /api/quant/lifecycle/transition
Change strategy lifecycle state

**Request:**
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "new_state": "PAPER",
  "reason": "Passed backtest validation",
  "approved_by": "admin"
}
```

**Response:**
```json
{
  "success": true,
  "previous_state": "RESEARCH",
  "new_state": "PAPER",
  "transitioned_at": "2025-12-22T12:00:00"
}
```

### Backtesting

#### POST /api/quant/backtest/run
Execute strategy backtest

**Request:**
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "universe": ["NIFTY"],
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "capital": 1000000,
  "params": {
    "or_duration_minutes": 15,
    "stop_loss_pct": 1.0
  }
}
```

**Response:**
```json
{
  "run_id": "bt_20241222_120000",
  "metrics": {
    "total_return": 23.5,
    "sharpe_ratio": 1.8,
    "max_drawdown": -12.3,
    "win_rate": 52.4,
    "total_trades": 156
  },
  "equity_curve": [
    {"date": "2024-01-02", "equity": 1002000},
    ...
  ]
}
```

### Research

#### GET /api/research/strategies
Get strategy list with backtest summaries

#### GET /api/research/strategy/{strategy_id}
Get detailed strategy analysis including:
- Drawdown forensics
- Equity curve
- Trade log
- "WHEN IT LOSES" documentation

---

## 10. Database Schema

### strategy_contracts
```sql
CREATE TABLE strategy_contracts (
    strategy_id VARCHAR(100) PRIMARY KEY,
    description TEXT,
    regime VARCHAR(50),  -- Momentum, MeanReversion, Volatility
    timeframe VARCHAR(20),  -- 5MIN, 1D
    holding_period VARCHAR(100),
    
    lifecycle_state VARCHAR(20) DEFAULT 'RESEARCH',
    when_loses TEXT NOT NULL,  -- MANDATORY
    allowed_universes JSON,
    parameters JSON,  -- Locked after LIVE
    
    state_since TIMESTAMP,
    approved_at TIMESTAMP,
    approved_by VARCHAR(50),
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_lifecycle ON strategy_contracts(lifecycle_state);
```

### backtest_runs
```sql
CREATE TABLE backtest_runs (
    run_id VARCHAR(100) PRIMARY KEY,
    strategy_id VARCHAR(100) REFERENCES strategy_contracts(strategy_id),
    universe_id VARCHAR(100),
    start_date DATE,
    end_date DATE,
    
    total_return FLOAT,
    sharpe_ratio FLOAT,
    max_drawdown FLOAT,
    win_rate FLOAT,
    total_trades INT,
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_strategy_run ON backtest_runs(strategy_id, created_at);
```

### backtest_daily_results
```sql
CREATE TABLE backtest_daily_results (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(100) REFERENCES backtest_runs(run_id),
    strategy_id VARCHAR(100),
    date DATE,
    
    equity FLOAT,
    drawdown FLOAT,
    daily_return FLOAT
);

CREATE INDEX idx_run_date ON backtest_daily_results(run_id, date);
```

### paper_orders, paper_trades, paper_positions
See Paper Trading System section for full schema.

---

## Conclusion

The Quant module implements **SmartTrader 3.0** with strict governance, research-first approach, and risk-centric design. Unlike traditional quant systems that optimize for returns, this module focuses on:

1. **Understanding failure modes** ("WHEN IT LOSES")
2. **Immutable strategy definitions** (no parameter tuning)
3. **Lifecycle governance** (RESEARCH → PAPER → LIVE)
4. **Drawdown-first risk management**
5. **Real-world validation** (paper trading before live)

**Key Takeaway:** This is NOT a "black box" optimizer. Every strategy must justify its existence, explain its risks, and prove its robustness through multiple validation stages.

**Future Enhancements:**
- Live broker integration
- Multi-asset strategies (options, futures)
- Machine learning signal generation
- Regime detection automation
