---
name: trading-safety
description: Rules for any change that can place, modify, cancel or account for an order. Load before editing order execution, risk, position sync, the Fyers broker plugin, the trading or terminal routers, or the frontend order ticket, and before writing tests for them. PAPER never touches the broker, live orders are idempotent and persisted first, and money math is exact.
user-invocable: false
---

# Trading safety

SmartTrader can place real orders through Fyers. A bug here loses money, so these rules hold even when a change looks unrelated.

## Code in scope

- `backend/app/services/order_execution_service.py`, `risk_manager.py`, `position_sync_service.py`
- `backend/app/brokers/**`, especially `brokers/plugins/fyers.py`
- `backend/app/routers/trading.py`, `terminal.py`, `unified.py`
- `frontend/app/terminal/**` (order ticket, trading mode)

## Rules

1. **PAPER never reaches the broker.** A PAPER code path must not construct, import-call or reach a live broker client. Branch on mode in one place, the execution venue, not in routers. When you touch a mode branch, add a test that fails if PAPER calls the broker.
2. **PAPER is the default.** Both the backend and the frontend default to PAPER. The UI never persists LIVE across reloads. Switching to LIVE needs an explicit confirmation that shows mode, symbol, side, quantity, order type and price.
3. **Live orders are idempotent.** Each live order gets a `client_order_id`. Persist the order as PENDING before calling the broker, then update it from the broker response. A retry with the same id returns the existing row and never places a second order.
4. **Every order field reaches the broker.** When mapping an order to Fyers, forward `trigger_price` for SL and SL-M orders, plus product type, validity and quantity. Add a test that asserts the exact payload sent for each order type.
5. **Risk checks run on every live path.** That covers place, modify, exit, square-off and options orders. Return warnings to the caller as well as failures. Never fall back to a hard-coded price; if no price is available, fail the check.
6. **Read quotes through one parser.** Fyers returns quote fields under `v` (`v.lp` and similar). Use the shared quote parser or `Quote` type, not ad hoc dict lookups.
7. **Fail closed when the broker is unavailable.** If the broker client is missing or a broker call fails, raise an error. Never return an empty list or zero that a caller could read as "no positions" or "order placed".
8. **Money math is exact.** Use `Decimal`, or integer paise, for prices, notional, P&L and charges. Round only at display or when building the broker payload, to the instrument's tick size.
9. **Live endpoints require identity.** Every route that can reach the live broker resolves the user from one dependency and checks ownership before cancel or modify. Never trust a raw `x-user-id` header alone.
10. **Never leak broker errors.** Log the full error server-side. Return a typed error code and message to the client, not `str(e)`.

## Testing

- Never call the real Fyers API from tests. Use a fake broker that records every call. If none exists yet, add one under `backend/tests/` and reuse it.
- Assert literal expected values, for example the exact payload, the stored order row, or the computed P&L. Do not assert only that a function was called.
- Run only the tests for the files you changed. CI runs the full suite.

## Before you finish

Check the diff against each rule above. List any rule a change could not meet and explain why in the PR description.
