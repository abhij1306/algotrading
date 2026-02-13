# SMARTTRADER 3.0 - SYSTEM BIBLE & AGENT GUIDE

This document serves as the comprehensive technical reference for the SmartTrader 3.0 backend. It outlines the architecture, module responsibilities, data flow, and interaction protocols.

---

## 🏗️ SYSTEM ARCHITECTURE OVERVIEW

SmartTrader 3.0 is a modular, agentic quantitative trading platform. The backend is built with **FastAPI** (Python 3.12+) and uses **PostgreSQL** as the single source of truth for all persistent state.

### Core Architecture Layers:
1.  **API Layer (FastAPI)**: Handles request routing, validation, and global exception handling.
2.  **Agentic Layer (Smart Trader)**: An autonomous system of specialized agents (Orchestrator, Risk, Execution, LLM Analyst).
3.  **Engine Layer (Quant & Research)**: High-performance logic for backtesting, strategy execution, and portfolio construction.
4.  **Service Layer**: Infrastructure services for market data, broker connectivity (Fyers), and paper trading.
5.  **Data Layer (SQLAlchemy)**: Manages persistence of historical data, signals, orders, and portfolio state.

---

## 📦 CORE BACKEND MODULES

### 1. Smart Trader (Agentic Brain)
Located in `backend/app/smart_trader/`. This is the system's "active" component.

*   **`NewOrchestratorAgent`**: The central coordinator.
    *   Runs a `_scanner_loop` during market hours.
    *   Triggers `_scan_cycle` which iterates through symbols.
    *   Coordinates the flow from Snapshot → Deterministic Generators → Aggregator → LLM Analysis → Confidence Scoring → Execution.
*   **Generators (`generators/`)**: Deterministic technical signal generators.
    *   `MomentumGenerator`, `VolumeAnomalyGenerator`, `RangeExpansionGenerator`, etc.
*   **Specialized Agents (`agents/`)**:
    *   `LLMSignalAnalyst`: Enhances signals with narrative reasoning.
    *   `ConfidenceEngine`: Combines deterministic and LLM inputs into a final score.
    *   `TradeConstructionAgent`: Calculates entry, SL, and target levels.
*   **`RiskAgent`**: The mandatory gatekeeper.
    *   Validates every trade against: Daily trade limits, Daily loss limits, Symbol cooldown, and Risk/Reward ratios.
    *   Calculates dynamic position sizing based on account capital and risk % per trade.
*   **`ExecutionAgent`**: The broker abstraction.
    *   Handles the lifecycle of orders.
    *   Supports `PAPER` and `LIVE` modes.
    *   Delegates to `PaperBroker` (persists to DB) or `FyersBroker` (Live API).

### 2. Research & Quant Engines
Located in `backend/app/engines/`.

*   **`StrategyExecutor`**:
    *   Runs a backtest loop over a specific `StockUniverse` and date range.
    *   Enforces capital constraints and persists `BacktestDailyResult` (equity, drawdown, returns).
*   **`PortfolioConstructor`**:
    *   Aggregates strategy-level results into a master portfolio.
    *   Implements allocation methods: `EQUAL_WEIGHT`, `INVERSE_VOLATILITY`, `CORRELATION_PENALIZED`.
    *   Enforces `PortfolioPolicy` (max exposure, cash reserves).
*   **`UniverseManager`**: Manages immutable stock universe definitions with historical membership tracking.

### 3. API Routers
Located in `backend/app/routers/`.

*   **`unified.py`**: A simplified, high-level API for placing orders and checking status across any broker mode.
*   **`portfolio_live.py`**: Powers the Monitoring UI. Provides "Live State", "Trust Maps" (drift analysis), and "Risk Summaries".
*   **`market.py` / `market_dashboard.py`**: Real-time market overview, sentiment, and global indices.
*   **`screener.py`**: High-performance stock screening based on 15+ technical and financial indicators.

### 4. Infrastructure Services
Located in `backend/app/services/`.

*   **`MarketDataService`**: Centralized fetching for global indices, CNN Fear & Greed, and India Sentiment (Tickertape/VIX).
*   **`FyersClient`**: Singleton adapter for the Fyers API v3.
*   **`LiveMarketService`**: Subscribes to WebSockets for real-time price updates.

---

## 🗄️ DATABASE SCHEMA (THE SOURCE OF TRUTH)

The system enforces a **Database-First** philosophy. No critical state (positions, orders, results) is kept solely in memory.

### Master Tables:
*   **`companies`**: Master list of NSE stocks.
*   **`historical_prices`**: Daily OHLCV + Technical Indicators.
*   **`intraday_candles`**: 5-minute candles for backtesting and agent snapshots.

### Trading & Agent Tables:
*   **`smart_trader_signals`**: Comprehensive log of every signal generated, including LLM narrative and confidence scores.
*   **`paper_orders` / `paper_trades` / `paper_positions`**: Full ledger for simulated trading.
*   **`agent_audit_logs`**: Step-by-step trace of agent decisions for transparency.
*   **`action_center`**: Actions requiring human intervention (e.g., high-risk order approval).

### Quant & Governance Tables:
*   **`strategy_contracts`**: Defines the "Contract" for a strategy (Allowed universes, timeframe, regime).
*   **`portfolio_policies`**: Governance rules (e.g., "Max 80% Equity Exposure").
*   **`backtest_runs`**: Snapshots of backtest configurations and aggregate results.
*   **`backtest_daily_results`**: Normalized daily output for individual strategies.
*   **`portfolio_daily_results`**: Aggregated daily output for multi-strategy portfolios.

---

## 🔄 CORE INTERACTION FLOWS

### 1. Signal-to-Trade Flow (Live/Paper)
1.  `Orchestrator` fetches 5m data (check DB cache → fallback Fyers API).
2.  `Generators` produce raw signals.
3.  `LLMSignalAnalyst` adds "why" narrative.
4.  `ConfidenceEngine` scores the signal (0.0 to 1.0).
5.  If High Confidence → `TradeConstructor` builds the setup (Entry/SL/Target).
6.  `RiskAgent` validates the setup against current portfolio risk.
7.  `ExecutionAgent` dispatches to Broker.
8.  Result is persisted to `smart_trader_signals` and `paper_trades` (if Paper).

### 2. Quant Research Flow
1.  User selects a `StockUniverse` and multiple strategies.
2.  `StrategyExecutor` runs daily backtests for each strategy; results saved to `backtest_daily_results`.
3.  `PortfolioConstructor` reads those results and applies a `PortfolioPolicy` to calculate optimal weights and aggregate equity.
4.  Final results saved to `portfolio_daily_results` for UI charting.

---

## 🔐 GLOBAL CODE REVIEW PROTOCOL

You are a senior software engineer and security reviewer. Conduct rigorous, practical, and context-aware code reviews.

### Objectives:
→ Improve correctness, security, maintainability, and scalability.
→ Minimize unnecessary refactoring.
→ Produce actionable feedback.

### Mandatory Review Dimensions:
1.  **Security**: Secrets, injections, auth/authz, sensitive data exposure.
2.  **Logic & Correctness**: Boundary conditions, state consistency, concurrency, idempotency.
3.  **Architecture**: SRP, dependency direction, layer isolation, API contracts.
4.  **Testing**: Coverage, failure modes, mock realism.
5.  **Maintainability**: Cognitive complexity, naming, documentation, config centralization.
6.  **Performance**: I/O efficiency, caching, memory lifecycle, resource cleanup.

### Finding Format:
```markdown
### [Severity: High | Medium | Low] <Title>
**Location:** path/file.ts:L42-L58
**Category:** <Category>
**Problem:** Concise technical description.
**Impact:** System/User/Data impact.
**Recommendation:**
```ts
// Fix example
```
Rationale: Engineering justification.
```

### Risk Scoring:
| Dimension | Risk Level |
|-----------|------------|
| Security  | Low/Med/High |
| Stability | Low/Med/High |
| Maintainability | Low/Med/High |
| Scalability | Low/Med/High |
