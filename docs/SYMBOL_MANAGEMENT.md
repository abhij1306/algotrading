# Symbol Management

**Status:** Canonical
**Code source:** `backend/app/services/symbol_master.py`
**Last updated:** 2026-02-19

## Canonical Formats
- `DB_FORMAT`: `SBIN`, `NIFTY50`
- `FYERS_FORMAT`: `NSE:SBIN-EQ`, `NSE:NIFTY50-INDEX`
- `DISPLAY_FORMAT`: same as DB format

## Core Rules
1. Internal storage + business logic use DB format.
2. Broker/provider boundaries use Fyers format.
3. UI displays DB format.
4. All conversions must use `symbol_master`; no ad-hoc string ops.

## Primary APIs
- `symbol_master.to_db(symbol)`
- `symbol_master.to_fyers(symbol)`
- `symbol_master.to_display(symbol)`
- `symbol_master.batch_to_db(symbols)`
- `symbol_master.batch_to_fyers(symbols)`
- `symbol_master.normalize(symbol, target_format)`
- `symbol_master.get_info(symbol)`
- `symbol_master.is_valid(symbol, format)`

## Alias and Index Handling
- Aliases normalized (examples):
  - `NIFTY` -> `NIFTY50`
  - `NIFTY BANK` / `NIFTYBANK` -> `BANKNIFTY`
  - `S&P BSE SENSEX` -> `SENSEX`
- Index provider mapping supported (example):
  - `BANKNIFTY` DB -> `NIFTYBANK` in Fyers ticker mapping

## Option/Future Helpers
- `to_fyers_option(underlying, expiry, strike, opt_type)`
- `to_fyers_future(underlying, expiry)`
- `parse_option_symbol(fyers_symbol)`

## Terminal and Options Mapping Examples
- Option board underlying:
  - Input UI symbol: `NIFTY`
  - Chain source symbol: `NSE:NIFTY50-INDEX` (resolved by service mapping)
- Equity order:
  - UI/API symbol: `SBIN`
  - Broker symbol: `NSE:SBIN-EQ`
- Option contract order:
  - UI components: `underlying=NIFTY`, `expiry=2026-02-26`, `strike=22500`, `option_type=CE`
  - Broker symbol build: `symbol_master.to_fyers_option(...)`
- WebSocket subscriptions:
  - Accepts `SBIN` and `NSE:SBIN-EQ`
  - Both normalize to DB with `symbol_master.to_db` before subscription tracking

## Where Conversions Must Happen
- Routers:
  - WebSocket subscribe/unsubscribe normalize inbound symbols via `to_db`
  - Provider subscriptions convert outbound via `to_fyers`
- Services:
  - `live_market_service` normalizes incoming provider ticks to DB symbol before broadcast

## Anti-Patterns (Forbidden)
- Manual replacement/parsing:
```python
symbol.replace('NSE:', '').replace('-EQ', '')
f"NSE:{symbol}-EQ"
symbol.split(':')[1]
```

## Validation Expectations
- Uppercase canonical symbols
- Expected DB symbol length <= 20
- Index/equity series resolved by symbol master metadata
- Unknown/invalid symbols should be rejected or skipped explicitly

## Documentation Drift Rule
If symbol behavior changes in `symbol_master.py`, update this file and references in the same change.
