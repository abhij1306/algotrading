# Universe Management

**Status:** Canonical
**Last updated:** 2026-03-15

## Purpose
Universe management is a platform service, not a screener or strategy detail.

It defines:
- what a universe is
- which symbols belong to it
- when they belonged to it
- what their weights were
- how to expose that information consistently to all features

## Canonical Entities
- `index_universe_definitions`
- `index_constituents_history`
- `index_weightage_changes`
- `universe_snapshots`

## Core Rules
1. Universe membership must be date-effective.
2. Universe weights must be date-effective where source coverage exists.
3. Current live membership and historical membership are separate query modes.
4. A current-membership fallback is allowed only as an explicit policy choice.
5. Features must not directly read raw CSV constituent files; they must use this canonical service.

## Canonical Inputs
- `data_system/03_universe/constituents/*.csv`
- `data_system/03_universe/monthly_universe_raw/<month>/<universe>.csv`
- `data_system/01_sources/nse_index_weights_pdf/**`

## Canonical Outputs
- DB-backed universe history tables
- daily universe membership datasets
- daily universe snapshots
- monthly weight datasets

## Required Service Capabilities
- `resolve_universe(universe_id: str, as_of_date: date, mode: str = "live") -> UniverseSnapshot | UniverseResult`
  Synchronous lookup. Raises `UniverseNotFoundError` when the universe id is unknown. May raise `NoDataAvailableError` when the universe exists but no membership is available for the requested date/mode. If indicative fallback mode is explicitly enabled, returns a fallback-labelled result instead of raising.
- `resolve_symbols(universe_id: str, as_of_date: date, mode: str = "live") -> list[str]`
  Synchronous lookup. Returns a deduplicated ordered symbol list. Raises `UniverseNotFoundError` for unknown universes. Returns an empty list only when the universe exists and the chosen policy explicitly allows empty membership instead of exception.
- `list_available_universes(active_only: bool = True) -> list[dict[str, object]]`
  Synchronous listing. Returns metadata records such as `index_code`, `name`, `description`, and `count`. Returns an empty list when no universes are seeded; does not raise by default.
- `get_universe_changes(universe_id: str, from_date: date, to_date: date) -> dict[str, object] | pd.DataFrame`
  Synchronous historical diff query. Raises `UniverseNotFoundError` for unknown universes and `InvalidDateRangeError` when `from_date > to_date`. Returns added/removed/unchanged constituents for the date range; if no changes exist, returns an empty result structure rather than raising.

## Query Modes
### Live mode
Use latest active constituents and weights.

### Historical mode
Use effective-date constituent membership and historical weights.

### Indicative fallback mode
If historical membership is unavailable before a source start date:
- allow current-membership fallback only when requested
- label output clearly as indicative

## Feature Rule
Strategies, backtests, screeners, and allocators must all consume the same universe service contract.

They may differ in policy:
- live mode
- historical mode
- indicative fallback mode

But they must not differ in underlying universe ownership.
