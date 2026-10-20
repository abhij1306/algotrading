# Architecture improvement plan

**Status:** Proposed, 2026-10-20
**Scope:** whole repo. The setup is about six months old, so every module, script and dependency is open to deletion or redesign.

This plan collects four area reviews. Each area file has the evidence (file and line references), target design, PR slices, risks and tests to add:

| Area | File |
|---|---|
| Live market data, WebSocket, Fyers broker | [live-market.md](live-market.md) |
| Orders, paper/live trading, risk, positions, DB and app wiring | [trading.md](trading.md) |
| Screener, VCP, backtests, universe, data access | [screener-backtest.md](screener-backtest.md) |
| Frontend | [frontend.md](frontend.md) |

The reviews were read-only and ran no code. Items marked "verify" in the area files came from grep or partial reads and need confirming before work starts.

## Themes across all areas

1. **Large amounts of dead code.** Several thousand lines have no importers. Examples: two `DataCache` classes, the whole `app/engines` and `app/strategies` trees, `app/models.py` (shadowed by the `app/models/` package), unused JWT auth, the backtest broker, and about 1,000 lines of unused frontend libraries and primitives. Deleting first makes every later step smaller.
2. **Several implementations of one concept.** There are four universe resolvers, four backtest metric calculators, at least two ATR definitions, three caches, three error hierarchies and three market-hours sources. Quote parsing exists in three places. Each concept gets one owner.
3. **Routers hold business logic.** SQL, caching, subscription diffing, paper-trading simulation and quote batching live in FastAPI routers. Move them into services so routers only map HTTP to calls.
4. **No clear seams.** The code relies on module singletons, raw `SessionLocal()` calls (57 sites), wall-clock reads and direct Fyers SDK access. That makes the core logic hard to test. Add small protocols where they unlock tests: `FyersGateway`, `DataSocket`, `Clock`, `PriceHistory`, `ExecutionVenue`.
5. **Few tests where money is at stake.** Order execution, the paper simulator, risk checks, position sync, VCP pattern detection and backtest metrics have no tests.

## Fix first: severe defects found during the review

These are bugs, not architecture work. They are small and should land before the refactors.

| Area | Defect |
|---|---|
| Trading | Risk manager reads `ltp`/`last_price` from a quote that uses `v.lp`, so every live MARKET order fails its price estimate. |
| Trading | Live stop-loss orders drop `trigger_price` and are sent to the broker as plain limit orders. |
| Trading | Position sync sets every stored position to zero when the Fyers client is unavailable. |
| Trading | Live orders are recorded only after the broker call, with no idempotency key. A failed commit followed by a retry can place a duplicate live order. |
| Trading | Square-off, exit, cancel and modify can reach the live broker without auth, ownership or risk checks. |
| Live market | After a provider reconnect, existing client subscriptions are not replayed, so live ticks silently stop. |
| Live market | Dropping a slow client leaks its provider subscriptions. `POST /api/websocket/disconnect` is undone by the monitor within 30 seconds. |
| Live market | After re-login, the broker and option-chain service keep the old Fyers client with the expired token. |
| Backtest | The VCP backtest resolves the universe once at the end date, which adds survivorship bias. |
| Frontend | The terminal remembers LIVE mode across reloads, with MARKET as the default order type. |
| Frontend | The `useWebSocket` close handler can tear down a newer socket when `enabled` changes or under StrictMode. |

## Recommended order

1. **Severe defects** above, each with a regression test.
2. **Deletion PRs** from every area. They are low risk and can run in parallel.
3. **Seams and test scaffolding:** a `FakeBroker`, a fake `DataSocket`, a `Clock`, and a session or unit-of-work dependency.
4. **One owner per concept:**
   - backend: subscription registry, Fyers gateway, universe service, backtest metrics and result type, indicators, settings, error hierarchy
   - frontend: API client, quote store, formatting and time
5. **Structural splits:** paper and live execution venues, `vcp_service.py` into a package, thin routers, data-access ports, and frontend feature folders with split pages.

Slices within each step are listed in the area files. Every slice is sized for one PR.

## Frontend direction

These owner decisions override anything in [frontend.md](frontend.md) that conflicts with them.

- **Design is owned by a separate agent.** Use shadcn/ui components organised by atomic design (atoms, molecules, organisms, templates, pages). Build on the Radix UI or React Aria primitives that shadcn uses. Replace the hand-rolled `components/ui` primitives rather than extending them.
- **No `useEffect`.** Derive values during render. Do work in event handlers. Fetch data with TanStack Query, which proposal 4 in frontend.md already recommends. Read the live socket through a store with `useSyncExternalStore` (zustand does this). Put redirects in the router. Oxlint currently reports 16 set-state-in-effect warnings; these are the starting worklist.
- **TypeScript best practices.** Keep `strict` on. Do not use `any`. Type backend contracts from FastAPI's OpenAPI schema (proposal 3) and validate at the network boundary. Use discriminated unions for request states. Await promises or mark them with `void`.

## Already done by the Vite+ migration

The migration branch `chore/vite-pnpm` covers part of frontend proposals 1, 6 and 10:

- **Tooling:**
  - Next.js is replaced by a Vite+ (`vp`) single-page app with React Router 8, managed by pnpm.
  - Oxlint runs with type-aware checks and Oxfmt formats the code.
  - Vitest runs through `vp test`, and the existing button test now runs.
- **Deletions:**
  - Electron packaging is removed. It pointed at an `electron/main.js` that never existed.
  - The unrendered `StockChart` and `PriceChart` components and the effect-based `/backtest/new` redirect page are deleted.
- **Fixes:**
  - UI barrel imports now use the correct file-name casing; the wrong casing broke on case-sensitive file systems.
  - The API and WebSocket now go through the Vite proxy on the same origin.

Still open on the frontend: the terminal's unrendered chart-data fetch, the remaining dead files and primitives, and everything from frontend.md proposal 2 onward.
