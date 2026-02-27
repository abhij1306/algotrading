# Terminal Trading Playbook

**Status:** Canonical
**Last updated:** 2026-02-20

## 1) Purpose and Scope
- Build a production-usable terminal with **PAPER + LIVE parity**, options-first UX, and shared execution engine.
- Instruments supported by same engine: Equity, Futures, Options (CE/PE).
- Phase-1 order types: `MARKET`, `LIMIT`, `SL`, `SL-M`.
- LIVE orders require explicit confirmation acknowledgment and risk pass before dispatch.

## 2) Execution Architecture
- Frontend Terminal UI -> FastAPI terminal/trading endpoints -> `order_execution_service` -> broker (`FyersBroker`) or simulator.
- PAPER path:
  - Uses same request shape as LIVE path.
  - Never dispatches to broker APIs.
  - Fills are simulated by paper rules.
- LIVE path:
  - Requires valid token.
  - Requires `is_live_confirmation_ack=true`.
  - Requires risk pass (or warning override reason where applicable).
- Symbol boundary:
  - Inbound normalize to DB format with `symbol_master.to_db`.
  - Outbound broker symbols built with `symbol_master.to_fyers` / option helpers.

## 3) Data Sources Matrix (Fyers)
- Quotes/Ticks:
  - Source: Fyers quotes + websocket stream.
  - Use: LTP, change, PnL refresh.
- Depth:
  - Source: Fyers market depth endpoint.
  - Use: bid/ask ladder summary and spread.
- Option Chain:
  - Source: Fyers optionchain via `option_chain_service`.
  - Use: strikes, OI, IV, option LTP, ATM context.
- Orders/Tradebook/Positions/Funds:
  - Source: Fyers broker endpoints (LIVE) + DB state.
  - Use: panel parity and reconciliation.
- Orderflow (Phase-1 definition):
  - Derived from depth + option chain OI/volume/PCR deltas (not true tape analytics).

## 4) API Contracts

### Existing reused contracts
- `/api/trading/mode`
- `/api/trading/order`
- `/api/trading/orders`
- `/api/trading/positions`
- `/api/trading/tradebook`
- `/api/trading/funds`
- `/api/options/chain`
- `/api/options/expiries`
- `/api/options/atm`
- `/api/options/greeks`

### Terminal options contracts
- `GET /api/terminal/options/board`
  - Returns underlying, expiry, ATM, spot, compact strike rows, freshness metadata.
- `GET /api/terminal/options/depth`
  - Returns depth snapshot for selected option contract or underlying.
- `GET /api/terminal/options/orderflow`
  - Returns derived metrics (`pcr_oi`, `pcr_volume`, oi totals, volume totals, spread summary).
- `POST /api/terminal/options/preview-order`
  - Returns normalized order payload, estimated notional, estimated charges, risk pre-check.
- `POST /api/terminal/options/order`
  - Terminal alias/wrapper to `/api/trading/order` with options-first defaults.

### Deprecation
- `POST /api/terminal/paper/order` is legacy compatibility only.
- Target: retire after terminal parity migration to `/api/trading/order`.

## 5) Paper Fill and Cost Model
- Fill rules:
  - `MARKET`: fill at simulated touch with spread + slippage bps.
  - `LIMIT`: fill when limit is marketable; otherwise remain submitted.
  - `SL`: trigger check then convert to limit behavior.
  - `SL-M`: trigger check then market-fill behavior.
- PnL includes estimated charges (config driven).
- Audit fields to preserve on order lifecycle events:
  - `fill_source` (`paper_sim` / `broker`)
  - `slippage_bps`
  - `estimated_charges`
  - `risk_override_reason` (when warning override used)
  - `is_live_confirmation_ack`

## 6) UI and UX Specification
- Default terminal layout is options-first:
  - Underlying selector, expiry selector, option chain board, depth/orderflow panel, quick order ticket.
- Keep existing watchlist and bottom account panels.
- Every order/position/trade row shows explicit mode badge (`PAPER`/`LIVE`).
- Mandatory states:
  - loading
  - empty
  - stale/degraded
  - hard error with actionable message
- LIVE order flow includes confirmation modal/step prior to submit.

## 7) Risk and Safety Rules
- Risk `FAIL`: hard block.
- Risk `WARNING`: allow only with explicit override reason.
- LIVE submit blocked if confirmation ack missing.
- Token/session invalid => block LIVE order dispatch.

## 8) Polling and WebSocket Update Model
- Option board and depth polling target: ~1.5s.
- Orders/positions/funds/trades polling target: 5-10s.
- WebSocket:
  - LTP updates for selected symbols/contracts.
  - Position PnL refresh using live ticks.
- Freshness:
  - Responses include timestamp where possible.
  - UI marks stale when age breaches threshold.

## 9) Acceptance Criteria
- PAPER and LIVE both submit through unified payload shape.
- PAPER never calls broker order placement APIs.
- LIVE always enforces confirmation and risk behavior.
- Options board renders chain + depth + orderflow for selected expiry.
- Mode toggle changes execution path only, not form shape.
- Bottom panels maintain paper/live clarity and data freshness labels.
