# Project Context: Agentic Smart Trader

This document provides a deep technical overview of the current system state, intended for AI agents or new developers joining the project.

## 1. System Identity
- **Name**: SmartTrader v2.1 (Agentic Upgrade)
- **Goal**: Deterministic, scalable, and model-agnostic algorithmic trading system for Indian Equities (NSE).
- **Core Philosophy**: "Fast Lane" for obvious signals, "Deep Reasoning" for complex ones. No black boxes.

## 2. Tech Stack hierarchy
- **Frontend**: Next.js 14 (App Router), TailwindCSS, Lucifer (Charts), Socket.IO Client. as a dashboard for the backend
- **Backend**: FastAPI (Python 3.10+), SQLAlchemy (Async), LiteLLM (AI), Socket.IO Server. as the main logic center
- **Database**: PostgreSQL (TimescaleDB ready) for market data, SQLite (for caching/session).
- **Broker Integration**: Fyers API (Primary), Paper Trading (Local Simulation).

## 3. Agentic Architecture (New in v2.1)

### A. The "Fast Lane" Loop
- **Purpose**: Bypasses LLM latency for high-confidence technical setups.
- **Component**: `FastTradingLoop` (`backend/app/smart_trader/fast_loop.py`).
- **Logic**:
  1. `StockScannerAgent` fetches 50 stocks in parallel (`ThreadPoolExecutor`).
  2. Computes Momentum Score (RSI + MACD + Volume).
  3. If `Score > 80`: Direct execution via `UnifiedTradingAPI`.
  4. If `Score 50-80`: Handover to `LLMClient` for narrative analysis.

### B. Model Agnosticism (LiteLLM)
- **Component**: `LLMClient` (`backend/app/smart_trader/llm_client.py`).
- **Capabilities**:
  - Switches instantly between `groq/llama-3`, `gpt-4`, `claude-3-opus`.
  - Configured via `.env` (`MODEL_PROVIDER`, `LITELLM_MODEL`).
  - Supports JSON mode enforcement for structured reasoning.

### C. Unified Trading API (The "Switch")
- **Component**: `ExecutionAgent` -> `UnifiedTradingAPI`.
- **Feature**: Global toggle `TradingMode` (PAPER vs LIVE).
- **Benefit**: Strategy logic is identical for both modes; only the final routing layer changes.

## 4. Current Roadmap (OpenAlgo Adaptation)
We are in the process of adopting patterns from OpenAlgo to improve robustness.

- **Phase 1: Persistence** (Next Step)
  - Replace in-memory paper trading with `sandbox_db` (SQLite).
  - Add `AuditLog` for agent decisions.
- **Phase 2: Action Center**
  - Implement a "Pending Orders" queue for human-in-the-loop approval of low-confidence signals.
- **Phase 3: Broker Plugins**
  - Refactor hardcoded Fyers logic into a dynamic plugin system.

## 5. Key File map
- `backend/app/smart_trader/new_orchestrator.py`: Main brain coordinating agents.
- `backend/app/smart_trader/stock_scanner.py`: Parallel data fetcher.
- `backend/app/smart_trader/fast_loop.py`: High-frequency loop.
- `backend/app/smart_trader/llm_client.py`: LiteLLM wrapper.
- `frontend/components/Terminal.tsx`: Real-time log and order viewer.

## 6. Environment Variables
Required for full functionality:
```bash
DATABASE_URL=postgresql://...
FYERS_ACCESS_TOKEN=...
GROQ_API_KEY=...
MODEL_PROVIDER=groq
LITELLM_MODEL=groq/llama-3.1-70b-versatile
```
