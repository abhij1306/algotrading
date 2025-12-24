# AGENTS.md

## 🤖 System Architecture & Guidelines for AI Agents

Welcome, Agent. This document defines the architecture, conventions, and operational rules for the "Jules" Algotrading System (v1.0).

### 1. Core Principles
1.  **Database is Truth:** All state (Strategies, Signals, Portfolio) must be persisted to PostgreSQL via SQLAlchemy models in `backend/app/database.py`.
2.  **Incremental Data:** Do not fetch historical data from Fyers API blindly. Check `IntradayCandle` table first. Use `DataProvider` or `NewOrchestratorAgent`'s caching logic.
3.  **Risk First:** All trades must pass `RiskAgent.check_trade()`. Respect the **Circuit Breaker** status.

### 2. Architecture Overview

#### 🧠 Smart Trader (Orchestrator)
- **Location:** `backend/app/smart_trader/new_orchestrator.py`
- **Role:** Generates signals using deterministic generators and LLM analysis.
- **Key Methods:**
    - `_scan_cycle()`: Main loop. Scans Stocks AND Options.
    - `execute_trade()`: Handles Paper/Live execution via `ExecutionAgent`.

#### 🛡️ Risk Management
- **Location:** `backend/app/smart_trader/risk_agent.py`
- **Features:**
    - **Circuit Breaker:** Checks API health and Global PnL.
    - **Cooldowns:** Enforces symbol-specific cooldowns.

#### 📡 Data Layer
- **Client:** `backend/app/services/fyers_client.py` (Singleton, handles Retries & Rate Limits).
- **WebSocket:** `backend/app/services/fyers_websocket.py` (Thread-safe broadcasting).

#### 🧪 Quant Engine
- **Runner:** `backend/app/engines/backtest/quant_wrapper.py`
- **Philosophy:** Strategies are locked "Contracts" (`StrategyContract`). No parameter tuning allowed during execution.

### 3. Developer Guidelines
- **Asyncio:** Be careful with mixing Sync/Async code. Use `asyncio.run_coroutine_threadsafe` when bridging threads.
- **Migrations:** If you modify `database.py`, ensure schema compatibility.
- **Testing:** Use `backend/scripts/verify_backtest.py` to verify core logic without live API.

### 4. Branching Strategy
- **main:** Production-ready code.
- **feature/**: New capabilities.
- **fix/**: Bug fixes.

*Maintain this document as the system evolves.*
