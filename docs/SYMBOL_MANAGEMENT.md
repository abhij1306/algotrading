# Symbol Management

**Status:** Canonical
**Code source:** `backend/app/services/symbol_master.py`
**Last updated:** 2026-03-15

## Purpose
Symbol management is part of the platform backbone, not part of any feature.

It must remain:
- provider-agnostic in design
- provider-specific only at the adapter boundary
- lifecycle-aware
- reusable by any future feature

## Core Roles
1. Symbol normalization
- Convert inbound symbols into canonical DB format.

2. Provider mapping
- Convert canonical symbols to provider-specific format like FYERS.

3. Symbol identity continuity
- Preserve meaning across renames, series changes, and lifecycle events.

4. Feature isolation
- Features consume symbol services and must not parse symbols ad hoc.

## Canonical Formats
- `DB_FORMAT`: `SBIN`, `NIFTY50`
- `FYERS_FORMAT`: `NSE:SBIN-EQ`, `NSE:NIFTY50-INDEX`
- `DISPLAY_FORMAT`: currently the same as DB format

## Core Rules
1. Internal storage and business logic use DB format.
2. Provider calls use provider format.
3. UI displays DISPLAY_FORMAT, which currently equals DB format.
4. All conversions must use `symbol_master`.
5. Lifecycle-aware alias handling must remain centralized, including display-facing aliases.

## Primary APIs
- `symbol_master.to_db(symbol)`
- `symbol_master.to_fyers(symbol)`
- `symbol_master.to_display(symbol)`
- `symbol_master.batch_to_db(symbols)`
- `symbol_master.batch_to_fyers(symbols)`
- `symbol_master.normalize(symbol, target_format)`
- `symbol_master.get_info(symbol)`
- `symbol_master.is_valid(symbol, format)`

Current display behavior:
- `DISPLAY_FORMAT` is not separately overridden in the current implementation.
- `symbol_master.to_display(symbol)` resolves through the same canonical identity as `to_db(symbol)`.
- Code reference: `backend/app/services/symbol_master.py` (`SymbolFormat.DISPLAY_FORMAT`, `SymbolInfo.display_format`)

If a future override is introduced, it must be implemented centrally in `symbol_master` and documented here with concrete examples before UI code may rely on it.

## Required Future Platform Contracts
The system should converge on a broader symbol platform interface:
- `resolve_symbol(symbol, provider, as_of_date)`
- `get_symbol_lineage(symbol, as_of_date)`
- `map_symbol_to_provider(symbol, provider, as_of_date)`
- `map_provider_symbol_to_db(provider_symbol, provider, as_of_date)`

This is how future features should consume symbol identity.

## Alias and Index Handling
Examples of canonical normalization:
- `NIFTY` -> `NIFTY50`
- `NIFTY BANK` / `NIFTYBANK` -> `BANKNIFTY`
- provider index aliasing may differ from DB identity

Example:
- DB: `BANKNIFTY`
- provider mapping may use: `NIFTYBANK`

This distinction belongs in symbol management, not in strategies or UI pages.

## Symbol Lifecycle Scope
Lifecycle management is closely related to symbol management and should remain date-effective.

It must cover:
- rename
- merger
- demerger
- delisting
- relisting
- series migration
- successor/predecessor linkage

Examples:
- old symbol -> new symbol
- old entity split into multiple successors
- trading series changes from `EQ` to `BE` or vice versa

## Lifecycle Rules
1. Lifecycle events must be queryable by date.
2. Features must not hardcode one-off alias maps.
3. If a symbol changed identity, the resolution policy must be explicit:
- current live identity
- historical identity as of date
- successor mapping

## Option and Future Helpers
- `to_fyers_option(underlying, expiry, strike, opt_type)`
- `to_fyers_future(underlying, expiry)`
- `parse_option_symbol(fyers_symbol)`

These helpers are still part of provider mapping and must not bypass canonical normalization rules.

## Where Conversions Must Happen
### Routers
- normalize inbound symbols to DB format
- convert outbound provider symbols only through symbol services

### Services
- live ticks normalize to DB symbol before broadcast
- order execution converts DB symbol to provider format at broker boundary
- backtests and scanners consume DB symbols only

### Data pipelines
- provider ingesters normalize into DB symbol identity before storing canonical datasets
- historical lineage decisions must be explicit and auditable

## Forbidden Anti-Patterns
Manual symbol manipulation is forbidden:

```python
symbol.replace('NSE:', '').replace('-EQ', '')
f"NSE:{symbol}-EQ"
symbol.split(':')[1]
```

Also forbidden:
- feature-local alias dictionaries
- strategy-specific rename handling
- provider-specific parsing embedded in UI code

## Validation Expectations
- uppercase canonical symbols
- explicit provider exchange/series metadata
- index/equity differentiation handled centrally
- unknown symbols rejected or marked unresolved explicitly
- lifecycle-driven mappings logged when they alter symbol identity

## Architecture Rule
If symbol behavior changes in `symbol_master.py` or lifecycle mapping logic, this document and the database/universe playbook must be updated in the same change.
