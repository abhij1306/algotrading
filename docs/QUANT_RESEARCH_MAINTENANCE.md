# Quant Research Module Maintenance Guide

This document serves as the source of truth for maintaining and extending the Quant Research (Portfolio-of-strategies) module. It details the strict contracts between the Database, Engine, and Frontend to prevent "NO UNIVERSE" or "Execution Failed" errors.

---

## 1. The Universe Contract

The most common failure point is a mismatch in **Universe Identifiers**.

### The Dependency Chain
A universe ID must be identical in **three** places:

1.  **Database (`stock_universes` table)**:
    - Primary key `id` (e.g., `NIFTY50_CORE`).
2.  **Code Strategy Contracts**:
    - File: `backend/app/engines/strategy_contracts.py`
    - Attribute: `allowed_universes` list inside `StrategyContract` definition.
3.  **Database (`strategy_contracts` table)**:
    - JSON column `allowed_universes`.

> [!IMPORTANT]
> If you add a new universe (e.g., `SMALLCAP_250`) to the database, you **MUST** update the `allowed_universes` list for every strategy in `strategy_contracts.py` that should be compatible with it.

---

## 2. Strategy Implementation Contract

All Quant strategies reside in `backend/app/engines/strategies/nifty_strategies.py`.

### Inheritance & Initialization
Every strategy MUST:
- Inherit from `BaseStrategy`.
- Support the standard `__init__` signature:
  ```python
  def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
      super().__init__(strategy_id, universe_id, parameters)
  ```
- Implement the `run_day` method:
  ```python
  def run_day(self, current_date: date, symbols: List[str], data_provider: Any) -> Dict[str, Any]:
      # Logic here...
      return {"signal": 0 or 1}
  ```

### Adding a New Strategy
1.  Define the class in `nifty_strategies.py`.
2.  Register the class in `backend/app/engines/strategies/__init__.py`.
3.  Define the `StrategyContract` in `backend/app/engines/strategy_contracts.py`.

---

## 3. Data Flow Architecture

The `QuantBacktestRunner` handles vectorized execution over a date range:

1.  **Selection**: Frontend sends `universe_id` and list of `strategy_id`s.
2.  **Validation**: Runner checks if `universe_id` is in the strategy's `allowed_universes`.
3.  **Persistence**: A `BacktestRun` record is created in the DB with `status="COMPLETED"` upon success.
4.  **Loop**: The runner calls `run_day()` for every date, gets signals, and calculates PnL.

---

## 4. Troubleshooting Guide

### "NO UNIVERSE" / "0 STRATEGIES" on Frontend
- **Cause**: Backend server failed to start or crashed.
- **Check**: Look for `SyntaxError` in `strategy_contracts.py`. Ensure trailing commas and braces are correct.

### Strategy Clicks Not Working
- **Cause**: Universe ID mismatch between the selected universe and the strategy's allowed list.
- **Check**: Open Browser Console. If `/api/portfolio/compatible-strategies/{id}` returns an empty list, the naming doesn't match.

### "Method Not Allowed" (405) / "500 Internal Error"
- **Cause**: 
  - 405: You might be hitting a different app on the same port (e.g., SenstoSales vs AlgoTrading). Ensure `PYTHONPATH` is set correctly.
  - 500: Database record mismatch or missing columns in `BacktestRun`.
- **Fix**: Run `scripts/fix_db_universes.py` to sync database contracts with code definitions.

---

## 5. Maintenance Scripts

- `fix_db_universes.py`: Syncs the database `strategy_contracts` table with the definitions in `strategy_contracts.py`. Run this after changing universe names or adding new ones.
- `verify_quant.py`: Runs a standalone diagnostic of the data provider and strategy signal generation.
