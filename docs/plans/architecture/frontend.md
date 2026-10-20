# Frontend architecture improvement plan (Vite + pnpm SPA)

> **Read first:** the owner's frontend direction in [README.md](README.md#frontend-direction) overrides this file where they conflict. Use shadcn with atomic design, no `useEffect`, and strict TypeScript; a separate agent owns design. Line numbers below predate the Vite+ migration and the Oxfmt reformat, so search by symbol name.

Scope: frontend/{app,components,hooks,lib,stores,utils}. No repo edits were made. Evidence comes from reading the code and grepping. Proposals are ranked by value/effort, best first. Sizes: S under 1 day, M 1-3 days, L more than 3 days.

## Findings that shape the plan

- Terminal page makes 8 raw `fetch('/api/...')` calls (frontend/app/terminal/page.tsx:291, 360, 401, 451, 572, 615, 722, 756). No shared client, no shared error shape. Order placement at :722 hardcodes `?x_user_id=default_user`. components/charts/StockChart.tsx:117 also uses raw fetch.
- Other pages use `apiClient` or the `screenerAPI`/`marketAPI`/`strategiesAPI` wrappers in lib/api-client.ts, but most `apiClient.get(...)` calls still hardcode URL strings in the page (dashboard/page.tsx:388-436, backtest/page.tsx:90-176, InsightsPanel.tsx:100-161, LoginButton.tsx:29-125).
- `lib/api-client.ts` reads errors as `{error:{code,message}}`, while terminal reads FastAPI `{detail}`. Check the backend shape (`SmartTraderException.to_dict()`, backend/app/main.py:253) and parse both. `marketAPI.searchSymbol` sends `?q=` but backend `search_symbols` takes `query` (dead wrapper today).
- `lib/types/api.ts` is imported only by `stores/marketStore.ts`, which nothing imports. Pages declare response interfaces inline: terminal 13, dashboard 10, screener 8, strategies 8, backtest 5. Nothing ties them to backend schemas.
- No data-fetching layer. Each page hand-rolls useState for data/loading/error plus useEffect and setInterval. useState counts: terminal 30, strategies 17, backtest 16, screener 16, dashboard 10. Polling lives in terminal (x2), dashboard, screener, backtest results, InsightsPanel. None cancels stale responses.
- `useWebSocket` is instantiated per page (dashboard :355, screener :345, terminal :229), so each navigation tears down and rebuilds the socket. Subscribe/unsubscribe diff logic is copy-pasted (terminal :489-523, screener :490-526, dashboard :470-498). Two consumption styles coexist: `lastMessage` state (re-renders the whole page per tick) and `registerCallback` (screener only). The zustand `marketStore` (liveQuotes, watchlist, isConnected) is unused.
- Formatting duplicated: `lib/utils.ts` and `utils/formatting.ts` both export `formatCurrency` and `formatPercentage` with different signatures (`utils/formatting.ts` is unused). Local copies: `formatDashboardNumber`/`formatDashboardPercent` (dashboard :73/:78), `formatCurrency`/`formatPct` (strategies :167/:172), `formatVolume`/`formatMarketCap` (screener :75/:82), `formatLtp` (terminal :163), `formatCompactCapital` (backtest :59), `formatRupeeLakh` (backtest results :60).
- Market hours has three sources of truth: `lib/market-hours.ts` (hand-rolled IST offset), `components/MarketStatus.tsx` (re-implementation, unused), and dashboard using `/api/market/status`. `isHoliday()` uses local-timezone `getDay()` unlike `isMarketOpen()`.
- Dead code (no importers found): `lib/migration-map.ts` (549 lines of legacy Raycast CSS mapping), `lib/design-tokens.ts` (439), `utils/formatting.ts`, `stores/marketStore.ts`, `lib/types/api.ts`, `components/MarketStatus.tsx`, `components/Toast.tsx`, `hooks/useKeyboardShortcuts.ts`, `components/charts/StockChart.tsx` (verify; grep showed only self-matches), terminal `_PriceChart` dynamic import (:168), chart refs (:237-240) and the chart fetch effect (:340-377) that nothing renders. UI primitives exported from components/ui/index.ts with no JSX usage in app/ or components/: Checkbox, EmptyState, GlassCard, MetricCard, Price, PriceChange, ChangeBadge, ScrollArea, all Skeleton*, DenseTableRow.
- Oversized pages: terminal 1287 lines, screener 886, dashboard 729, strategies 617, backtest results 417. Each mixes types, formatters, data hooks, WebSocket handling and JSX.
- Testing: a single test (components/ui/__tests__/button.test.tsx); no visible runner config. Page logic is untestable because it lives inside components.

## Proposals (ranked by value/effort)

### 1. Delete dead code first (S, very low risk)
- Problem: roughly 1,300+ dead lines would be ported to Vite for nothing.
- Target: remove everything in the dead-code list above. Keep UI primitives only if DESIGN_SYSTEM.md wants them.
- Steps: PR1 delete zero-importer files (migration-map, design-tokens, utils/formatting, marketStore, types/api if superseded, MarketStatus, Toast, useKeyboardShortcuts). PR2 remove terminal `_PriceChart`, chart refs and chart fetch effect, then PriceChart/StockChart. PR3 remove unused UI primitives after a grep for JSX and import-name usage. Type check changed files only.
- Do this before or at the start of the Vite port.

### 2. Single typed API layer (M, low risk)
- Problem: scattered URLs, inconsistent error handling, no single base-URL config (`NEXT_PUBLIC_API_URL`/`NEXT_PUBLIC_WS_URL` become `import.meta.env.VITE_*`), `default_user` hardcoded, `{detail}` messages dropped by apiClient so users see "Request failed with status 400".
- Target: `src/api/` with `config.ts`, `client.ts` (base URL, AbortSignal, normalized `ApiError` that throws, parses both `{error}` and `{detail}`), and one module per backend router: market, trading, terminal, options, screener, strategies, backtest, auth, portfolio. Each function returns typed data and accepts `signal`. Order placement goes through one `placeOrder()` function so the LIVE-order path has a single audited call site.
- Steps: PR1 config + new client, old `apiClient` becomes a thin adapter. PR2 move terminal's 8 fetches and StockChart. PR3 dashboard + InsightsPanel string URLs. PR4 backtest, strategies, screener, auth; delete old wrappers.
- Size: M over 4 PRs. The Vite dev proxy (`/api`, including the websocket path) replaces the next.config rewrites.

### 3. Shared contract types (M, low risk)
- Problem: inline interfaces per page can drift from the backend.
- Target: generate `src/api/schema.d.ts` from FastAPI `/openapi.json` with `openapi-typescript` (`pnpm gen:api`). Page/domain types derive from it. For routes lacking `response_model`, add one in the backend or keep a hand-written type next to the endpoint function plus a runtime guard (lib/type-guards.ts exists).
- Steps: PR1 generator + committed output + CI regenerate-and-diff check. PR2-4 replace page-local interfaces as each page moves to the API layer. Risk: untyped backend routes limit coverage.

### 4. TanStack Query for fetching, polling and mutations (M-L, medium risk)
- Problem: hand-rolled loading/error/polling in every page, no cancellation, out-of-order responses possible. Terminal's options poll has an in-flight guard (:265, :387) that drops the refetch after underlying/expiry change (see "Severe issues").
- Target: `QueryClientProvider` at the app root. One hook per endpoint (`useWatchlist`, `useOptionsBoard(underlying, expiry)`, `usePositions`, `useScreenerResults(params)`, `useMarketStatus`, `useBacktestRun(id)`). Polling via `refetchInterval` as a function of market status, so market-open is re-evaluated each tick. Mutations (`placeOrder`, `squarePosition`, watchlist add/remove, strategy queue/cancel/halt) use `useMutation` plus invalidation. Query keys include all params so stale responses are discarded; pass `signal` to the API layer. Do not put live ticks in the query cache (see 5).
- Steps: PR1 provider + defaults (retry only network errors, short staleTime, no refetch-on-focus for trading data). PR2 terminal options board + positions. PR3 dashboard + InsightsPanel. PR4 screener. PR5 strategies. PR6 backtest and results polling (stop refetching when the job is done). Depends on proposal 2.

### 5. One WebSocket provider feeding a zustand quote store (M-L, medium risk)
- Problem: see findings (per-page sockets, copied subscription logic, whole-page re-render per tick, unused store). The hook also has a lifecycle bug (see "Severe issues").
- Target:
  - `src/realtime/socket.ts`: a framework-free `QuoteSocket` class (connect, capped backoff with jitter, heartbeat, handlers that ignore events from a stale socket, revive on online/visibility, an explicit "disconnected" state instead of silently giving up after 10 retries, reconnect also on server-initiated code 1000).
  - Ref-counted subscription registry: `useQuotes(symbols)` registers symbols; the registry sends only the aggregate subscribe/unsubscribe delta (matches the backend aggregate-delta design in AGENTS.md) and resubscribes everything on reconnect.
  - Ticks write into a zustand `quoteStore` (`Record<symbol, Quote>`), batched per animation frame. Components use `useQuote(symbol)` selectors so only the affected row re-renders. Screener flash animation reads store subscriptions or a raw tick callback. `isConnected` lives in the store.
  - Keep ticker normalization (`normalizeTicker`, ticker_batch handling) in the socket layer.
- Steps: PR1 extract `QuoteSocket` with unit tests (fake WebSocket: stale close, reconnect, resubscribe). PR2 provider + registry + `quoteStore` (reuse the shape of `marketStore`, delete the rest). PR3 terminal. PR4 dashboard. PR5 screener, then delete `useWebSocket.ts`.
- Risk: medium, since this is the live-data path. Migrate one page at a time; `GET /api/websocket/status` is the smoke test.

### 6. Feature-based folder structure and routing (M, low-medium risk; fold into the Vite port)
- Current: Next `app/` pages contain everything; components/ mixes layout, ui, dashboard, charts, terminal.
- Target:
  - `src/app/`: router, providers, AppShell, ErrorBoundary
  - `src/api/`: client, config, one module per router, generated schema
  - `src/realtime/`: socket, provider, registry, quoteStore
  - `src/features/{terminal,screener,dashboard,strategies,backtest,auth}/`: each with components/, hooks/, lib/, types
  - `src/shared/{ui,lib,hooks,stores}/`: design-system primitives (only used ones), format.ts, time.ts, cn
  - Rule: features import from shared/api/realtime, never from each other. Routes are lazy (`React.lazy`) in place of `next/dynamic`. Use react-router `useSearchParams` for the terminal `?symbol=` param (terminal :268 reads `location.search` once at mount, so in-app navigation will not update it).
- Steps: shell and shared pieces go in the Vite scaffold PR; then one feature per PR together with proposals 2-5 for that feature. Untouched pages can move as-is.

### 7. Split oversized pages (L, low risk if done after 4 and 5)
- Terminal: hooks `useTerminalWatchlist`, `useOptionsBoard`, `usePositions`, `useOrderTicket` (form state + placeOrder); components `WatchlistPanel`, `OptionChain`, `PositionsTable`, `OrderPanel`, `TerminalHeader`. The toolbar Buy/Sell buttons at :1173-1174 have no handler; remove or wire them. Move `inferInstrumentType`, `buildOptionsOrderflow`, lot sizes to `features/terminal/lib` with unit tests.
- Screener: `useScreenerQuery`, `useLiveQuoteOverlay`, `ScreenerTable`, `ScreenerFilters`; sort/format as pure functions.
- Dashboard: `useMarketStatus`, `useDashboardData`, `IndicesPanel`, `WatchlistPanel`, `PortfolioCards`.
- Strategies: `useStrategyData`, `ScanTable`, `PositionsTable`, `BacktestPanel`.
- Steps: one page per PR; extract pure functions and types first, then hooks, then components, with no behavior change per step.

### 8. Consolidate formatting and time utilities (S-M, low risk)
- Target: one `shared/lib/format.ts` (price, signed percent, compact INR with lakh/crore, volume, market cap) built on `Intl.NumberFormat('en-IN')`, and `shared/lib/time.ts` using `Intl.DateTimeFormat` with `timeZone: 'Asia/Kolkata'` for market hours and display. `isMarketOpen()` derives from the backend `/api/market/status` when available, with the local clock as fallback. Use IST in `isHoliday()` or delete it if unused. Delete `utils/formatting.ts`, `MarketStatus.tsx` and the per-page local formatters.
- Steps: PR1 add the module with table-driven unit tests (null, NaN, negative, zero, large values). PR2+ replace page-local copies page by page.

### 9. Safety wrapper for live orders (S, low risk, high value)
- Current (terminal/page.tsx): trading mode persists in localStorage (:276-284), so a user who last used LIVE reopens the terminal with LIVE armed and MARKET as the default order type. Confirmation is `window.confirm` showing side, quantity and symbol only (:715), not order type, price or product. The request body reads mode and form state at call time after the confirm.
- Target: default to PAPER on every load and never persist LIVE; replace `confirm` with an in-app dialog listing mode, symbol, side, quantity, type, price/trigger; build the request body once from the confirmed snapshot; add an idempotency key if the backend supports one; keep the single call site from proposal 2.
- Steps: PR1 mode default/persistence change. PR2 confirmation dialog component with a test that LIVE requires the ack and PAPER does not.

### 10. Testing strategy (M, enables the rest)
- Target: Vitest + React Testing Library + MSW (mock at the network boundary), jsdom. Priority order: format/time utils; `QuoteSocket` (reconnect, stale close event, resubscribe delta); order ticket (LIVE confirmation, request body, lot-size quantity for options); options board hook (changing underlying refetches, out-of-order responses ignored); screener sort/filter pure functions. Playwright smoke tests against a mocked backend later.
- Steps: PR1 Vitest config plus the format/time tests inside the Vite scaffold; add tests in the same PR as each extracted hook. Per repo rule, run only changed-file tests locally; CI runs the suite.

### 11. Design-system cleanup (S-M, low risk)
- Current: components/ui/index.ts exports about 25 components, many unused. Pages mostly use Button, Card, Badge, Input, PageContainer, and hand-style raw table/select/input elements (terminal throughout) instead of Table/Select/Input. Skeleton and EmptyState exist but pages use ad-hoc spinners and text.
- Target: after deleting unused primitives, use the remaining ones consistently where pages hand-roll them, following docs/DESIGN_SYSTEM.md. Add one `QueryBoundary` component (skeleton, error with retry, empty state) for all TanStack Query pages. Do this inside each page-split PR, not as a standalone effort.

## Suggested order of work

1. Proposal 1 (delete dead code) before or at the start of the Vite port.
2. Proposals 6 (shell), 8 (shared libs), 10 (test setup) folded into the Vite scaffold PRs.
3. Proposals 2 then 3, page by page.
4. Proposal 9 (order safety), small and independent; ship any time.
5. Proposals 5 and 4, page by page, terminal first (most state).
6. Proposals 7 and 11 alongside each page's migration.

## Severe issues noticed along the way (not the focus)

- hooks/useWebSocket.ts:252-258 with :292-300: when `enabled` flips or React StrictMode double-mounts, the previous socket's `onclose` fires after the new effect reset `cancelledRef` to false. It sets `socketRef.current = null`, clears the heartbeat interval and sets `isConnected=false` on the new connection, so sends fail and live data can stall. Dashboard uses `enabled: marketStatus.isOpen`, so it flips. Fix: `if (socketRef.current !== socket) return;` at the top of `onclose` (or the equivalent check in `QuoteSocket`). Severity: high.
- terminal/page.tsx:276-284 and :714: LIVE mode persists across sessions (proposal 9). Severity: high for a trading app.
- terminal/page.tsx:386-438: the in-flight guard drops the refetch after an underlying or expiry change and discards the old response as cancelled. The board shows the previous underlying for 3-20 seconds and `optionsLoading` can stay stuck. `optionsExpiry` is not reset when the underlying changes, so a NIFTY expiry can be sent for BANKNIFTY. Proposal 4 fixes this structurally. Severity: medium.
