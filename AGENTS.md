# SMARTTRADER 3.0 - SYSTEM BIBLE & AGENT GUIDE

This document serves as the comprehensive technical reference and "System Bible" for SmartTrader 3.0. It documents the entire system's capabilities, architecture, data flows, and identified discrepancies.

---

## 🎯 1. SYSTEM OVERVIEW & PHILOSOPHY

SmartTrader 3.0 is a modular, agentic quantitative trading platform designed for the Indian NSE market.

### Core Philosophy:
*   **Research explains risk — not returns**: The system prioritizes understanding and managing downside risk over chasing performance.
*   **Database as Single Source of Truth**: No critical state (positions, orders, results) is kept solely in memory. All state is persisted to PostgreSQL.
*   **Agentic Orchestration**: Specialized agents handle specific tasks (Scanning, Risk, LLM Analysis, Execution) coordinated by a central Orchestrator.
*   **Incremental Data Fetching**: Efficiency is achieved by checking the database cache before falling back to external APIs (Fyers/yFinance).

---

## 🏗️ 2. ARCHITECTURAL LAYERS

The system is organized into five distinct layers:

1.  **Layer 1: Data Storage (`nse_data/`, PostgreSQL)**:
    *   Historical price data, company metadata, signals, and trading records.
2.  **Layer 2: Data Pipelines (`data_platform/`)**:
    *   Scripts and processors for EOD updates, intraday candle ingestion, and index membership management.
3.  **Layer 3: Backend Services (`backend/app/services/`, `engines/`, `smart_trader/`)**:
    *   The "Brain" of the system. Logic for strategy execution, risk management, and agentic workflows.
4.  **Layer 4: API Layer (`backend/app/routers/`)**:
    *   FastAPI endpoints providing data and control to the frontend.
5.  **Layer 5: Frontend (`frontend/`)**:
    *   Next.js 16 (App Router) interface for monitoring, research, and trading.

---

## 📦 3. MODULE DEEP DIVE

### 3.1 Smart Trader (Agentic Brain)
Located in `backend/app/smart_trader/`.

*   **`NewOrchestratorAgent`**: The central coordinator. Manages the `_scanner_loop` during market hours (9:15 AM - 3:30 PM IST).
*   **Generators (`generators/`)**: Deterministic technical signal generators (Momentum, Volume Anomaly, Range Expansion, Reversal, Index Alignment).
*   **Specialized Agents (`agents/`)**:
    *   `LLMSignalAnalyst`: Adds narrative reasoning to signals using LLMs (Groq/OpenAI).
    *   `ConfidenceEngine`: Scores signals (0.0 to 1.0) based on deterministic and LLM inputs.
    *   `TradeConstructionAgent`: Builds entry, SL, and target setups.
*   **`RiskAgent`**: Validates every trade against daily limits, loss thresholds, and risk/reward ratios.
*   **`ExecutionAgent`**: Dispatches orders to the active broker (`PaperBroker` or `FyersBroker`).

### 3.2 Research & Quant Engines
Located in `backend/app/engines/`.

*   **`StrategyExecutor`**: Runs backtests over a stock universe.
*   **`PortfolioConstructor`**: Aggregates backtest results into portfolios using various allocation methods (Equal Weight, Inv Vol, etc.).
*   **`UniverseManager`**: Handles stock universes and historical membership.

### 3.3 Infrastructure Services
Located in `backend/app/services/`.

*   **`MarketDataService`**: Aggregates market overview, sentiment, and global indices.
*   **`FyersClient`**: Singleton adapter for Fyers API v3.
*   **`LiveMarketService`**: Broadcasts live ticks via WebSockets.
*   **`SymbolMaster`**: Standardizes symbols between `DB_FORMAT` (Ticker) and `FYERS_FORMAT` (Exchange:Ticker-Series).

---

## 🗄️ 4. DATABASE SCHEMA

### Key Tables:
*   **`companies`**: Master list of stocks.
*   **`historical_prices`**: Daily OHLCV + 15+ Technical Indicators (RSI, EMA, ATR, MACD, ADX, etc.).
*   **`intraday_candles`**: 5-minute candles used for scanning and backtesting.
*   **`smart_trader_signals`**: Log of all agent-generated signals and LLM narratives.
*   **`paper_orders` / `paper_trades` / `paper_positions`**: Full ledger for paper trading.
*   **`strategy_contracts`**: Definitions and governance rules for institutional strategies.
*   **`backtest_daily_results` / `portfolio_daily_results`**: Persistent results for research.

---

## 🔄 5. INTERACTION FLOWS & DATA LIFECYCLE

### 5.1 Signal-to-Trade Flow
1.  **Scanner** triggers → **Snapshot** built (DB -> Fyers fallback).
2.  **Generators** produce raw signals → **Aggregator** merges into Composite Signals.
3.  **LLM Analyst** adds "Why" → **Confidence Engine** scores (HIGH/MEDIUM/LOW).
4.  If **HIGH** → **TradeConstructor** builds setup → **RiskAgent** validates.
5.  **ExecutionAgent** places order via **Broker** → State persisted to DB.

### 5.2 Daily Update Process (4:00 PM IST)
1.  Fetch EOD prices for all companies.
2.  Update Index historical data.
3.  **Precompute Indicators**: Recalculate ATR, RSI, etc., for the entire universe (Crucial for Screener accuracy).

---

## 🎨 6. FRONTEND ARCHITECTURE & DESIGN SYSTEM

### Tech Stack:
*   Next.js 16 (App Router), React 19, Tailwind CSS 4.

### Design System (Tokens):
*   Defined in `globals.css` using CSS variables (e.g., `--color-primary`, `--color-profit`).
*   **Base Colors**: Dark theme (#0A0A0B background).
*   **Glass Morphism**: Used for Cards and Modals (`.glass-card`).

### Core Components (`components/ui/`):
*   `Card`: Flexible container with glass variants.
*   `Button`: Standardized variants (Primary, Secondary, Profit, Loss).
*   `Table`: High-density data tables with sticky headers.
*   `MetricCard`: Specialized for displaying trading metrics.

---

## 🔐 7. PROTOCOLS & STANDARDS

### 7.1 Global Code Review Protocol
Mandatory dimensions for all PRs: Security, Logic & Correctness, Architecture, Testing, Maintainability, Performance.

### 7.2 Symbol Format Rules
*   **`DB_FORMAT`**: "SBIN" (Internal storage, searches).
*   **`FYERS_FORMAT`**: "NSE:SBIN-EQ" (External API calls).
*   **Rule**: Always use `symbol_master.to_db()` before storing and `symbol_master.to_fyers()` before calling Fyers.

---

## ⚠️ 8. AUDIT FINDINGS & DISCREPANCIES

The following inconsistencies and technical debt were identified during the Feb 2026 audit:

1.  **File Organization**:
    *   `backend/app/smart_trader_api.py` and `backend/app/ai_insight_api.py` are located in the root of `backend/app/` instead of `backend/app/routers/`.
    *   Several legacy-style files (`screener.py`, `models.py`, `strategies.py`) remain in `backend/app/`, containing logic that has been partially superseded by newer services.
2.  **Redundant Logic**:
    *   `backend/app/database.py` and `backend/app/main.py` both contain logic for `Base.metadata.create_all()`.
3.  **Broker State**:
    *   `ExecutionAgent` in `update_positions` notes that `stop_loss` and `target` are not yet persisted in the `PaperPosition` table, relying on the strategy loop or manual exit for now.
4.  **Symbol Master Loading**:
    *   `SymbolMaster` uses a lazy-loading approach for `get_info` but the `_load_symbol_master` method is currently a placeholder.

---

## 🚀 9. DEVELOPER GUIDE

### Quick Start:
1.  **Backend**: `cd backend && python run_entry.py`
2.  **Frontend**: `cd frontend && npm run dev`
3.  **Full Stack**: `python start_dev.py`

### Critical Scripts:
*   `run_daily_update.bat`: Master script for daily data maintenance.
*   `backend/scripts/seed_strategies.py`: Populates the database with institutional strategies.
*   `backend/scripts/migrate_add_constraints.py`: Ensures database integrity constraints are applied.

---
**Version:** 3.0.0 (Agent Bible v1.0)
**Last Updated:** February 2026
**Status:** ✅ Audit Complete - Documentation Finalized
