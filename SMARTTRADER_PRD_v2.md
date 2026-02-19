# SmartTrader — Product Requirements Document (Final)

> **Version:** 2.0 — Supersedes all previous PRD versions
> **Last Updated:** 2026-02-19
> **Audience:** AI coding agents and human developers
> **Purpose:** Single source of truth for what SmartTrader is, what each module must do, and what the boundaries are. Read this entirely before modifying any code.

---

## 1. What This System Is

SmartTrader is a **single-user, personal Indian equity and F&O trading platform** built as both a personal trading tool and an investor pitch demonstrating institutional-grade engineering. It is a **GitHub open-source project** with no authentication, no multi-user architecture, and no external paid data vendors.

### The Pitch Positioning
An investor looking at this system should feel two things:

1. **Technical depth** — the data pipeline, backtesting methodology, and real-time architecture are non-trivial and demonstrate genuine engineering sophistication
2. **Institutional quality** — the UI and data presentation look and behave like a professional terminal, not a hobby project

### What It Is Not
- Not a multi-user platform (no auth, no roles, no sessions)
- Not a prop trading system yet — it is the **foundation** that could become one
- Not dependent on any paid data vendor — every data point comes from Fyers API or yfinance
- Not a place for dummy data — if real data is unavailable, show a clear empty/offline state with a reason

### Data Sources (Exhaustive List)
| Source | Used For | Access Method |
|---|---|---|
| Fyers API v3 (REST) | Live quotes, historical OHLCV, order placement, positions, order book, options chain | `fyers_apiv3` SDK |
| Fyers WebSocket | Real-time tick streaming during market hours | `FyersWebsocket.data_ws` |
| yfinance | Post-market global indices, commodities, US VIX | `yfinance` Python library |
| CNN Fear & Greed API | US market sentiment | Public HTTP endpoint |
| Tickertape (scrape) | India Market Mood Index (with VIX fallback) | HTTP scrape, VIX fallback |
| Internal PostgreSQL DB | Pre-computed technical indicators, company metadata, historical prices, symbol lifecycle | SQLAlchemy ORM |

**No other data sources are permitted.** Do not introduce new data vendors without explicit approval.

---

## 2. Development Philosophy

### Phase 1 (Current — 2–3 month target): Simple and Robust
Every module must first be **stable, correct, and data-complete** before adding features. Phase 1 is the foundation. An investor who sees a fast, clean, accurate v1 is more impressed than one who sees a feature-rich but broken system.

Phase 1 success criteria per module:
- Dashboard: Real data loads correctly, WebSocket ticks update live, post-market view works
- Screener: All 33 universes load, sorting and filtering work, live prices update
- Terminal: Chart loads, orders can be placed (live and paper), positions display correctly
- Backtest: Nifty 50 data pipeline is clean and verifiable, equity curve renders correctly

### Phase 2 (Future): Feature Expansion
Options chain, Greeks, strategy builder, multi-leg orders, OI scans, additional backtest universes, walk-forward optimisation. None of this is in scope until Phase 1 is stable.

### The "No Dummy Data" Rule
This is non-negotiable and applies to every module, every widget, every page:
- If Fyers is disconnected → show last known price with a "Delayed" or "Offline" label
- If market is closed → show the closed state clearly, switch to yfinance data where appropriate
- If a data fetch fails → show an empty state with the reason ("Unable to load — Fyers token may have expired")
- Never generate, hardcode, or fabricate numbers to fill a widget

---

## 3. Tech Stack

### Backend
| Layer | Technology |
|---|---|
| Framework | FastAPI (Python) |
| Database | PostgreSQL via SQLAlchemy ORM |
| Broker API | Fyers API v3 (`fyers-apiv3`) |
| Market Data Fallback | `yfinance` |
| WebSocket (Fyers) | `fyers_apiv3.FyersWebsocket.data_ws` (blocking, runs in daemon thread) |
| WebSocket (Frontend clients) | FastAPI native WebSocket |
| Task Model | asyncio event loop + daemon threads for blocking Fyers SDK calls |
| Timezone | All market time logic in IST (`Asia/Kolkata` via `pytz`) |

### Frontend
| Layer | Technology |
|---|---|
| Framework | Next.js 14 (App Router) |
| Language | TypeScript |
| Styling | Tailwind CSS + custom design system |
| State | React hooks — no global state library |
| WebSocket | Custom `useWebSocket` hook |
| Data Fetching | REST via typed `api-client.ts` |

### Key File Map
```
backend/
  app/
    routers/
      websocket.py            ← WebSocket API endpoint + REST helpers
      screener.py             ← Screener REST API
    services/
      live_market_service.py  ← WebSocket orchestrator, tick routing, market hours
      fyers_websocket.py      ← Fyers SDK wrapper (daemon thread)
      fyers_client.py         ← Fyers REST client (quotes, history, orders, options)
      market_data_service.py  ← yfinance integration (global indices, sentiment, ADX)
      symbol_master.py        ← Symbol format conversion (DB ↔ Fyers ↔ Display)
      symbol_lifecycle.py     ← Historical symbol resolution (mergers, renames, delistings)
    utils/
      ws_manager.py           ← Frontend WebSocket connection pool + broadcaster
    models/
      company.py              ← Company metadata
      historical_price.py     ← OHLCV + pre-computed technical indicators
      symbol_history.py       ← Symbol change events (mergers, renames, delistings)

frontend/
  hooks/
    useWebSocket.ts           ← WebSocket lifecycle, reconnect, heartbeat, callbacks
  app/
    dashboard/page.tsx        ← Dashboard
    screener/page.tsx         ← Screener
    terminal/page.tsx         ← Terminal
    backtest/page.tsx         ← Backtest
```

---

## 4. System-Wide Architecture

### 4.1 Symbol Format Convention

All format conversions happen at API/router boundaries via `symbol_master`. Internal code always uses DB format.

| Context | Format | Example |
|---|---|---|
| Database / Internal | Short uppercase | `SBIN` |
| Fyers SDK / WS subscribe | Provider format | `NSE:SBIN-EQ` |
| Frontend display | Short uppercase | `SBIN` |
| WebSocket subscribe message | Fyers format | `NSE:SBIN-EQ` |
| WebSocket broadcast / tick data | DB format | `SBIN` |

**Enforcement rule:** Pages send Fyers-format symbols in `subscribe` messages. `websocket.py` converts to DB format via `symbol_master.to_db()` before passing to `manager.subscribe()`. Ticks broadcast with DB-format symbols. Frontend receives `SBIN`, never `NSE:SBIN-EQ`.

### 4.2 WebSocket Architecture

The WebSocket pipeline is the core of real-time delivery. Understand this fully before modifying any WebSocket file.

```
Fyers SDK (blocking, daemon thread)
  → FyersWebSocketService._on_message(tick)
    → LiveMarketService.handle_tick_incoming(tick)   [direct call, no queue]
      → asyncio.run_coroutine_threadsafe(manager.broadcast(msg), loop)
        → ConnectionManager.broadcast()
          → per-client subscription filter
            → websocket.send_text(json_msg)
              → useWebSocket hook (frontend)
                → registered callbacks OR lastMessage state
```

**Tick flush:** Ticks are buffered for 500ms and broadcast as a `ticker_batch` message. The frontend hook unrolls batches into individual `ticker` messages before delivering to page components. Page components always receive single `ticker` messages.

**Market hours enforcement:**
- Fyers WebSocket connects only during market hours: 09:15–15:30 IST, Mon–Fri
- `_monitor_loop` checks every 30s, reconnects if dropped during hours
- `DEV_MODE=True` env var bypasses market hours for development

### 4.3 WebSocket Protocol

**Frontend → Backend:**
```json
{ "action": "subscribe", "symbols": ["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX"] }
{ "action": "unsubscribe", "symbols": ["NSE:SBIN-EQ"] }
"ping"
```

**Backend → Frontend:**
```json
{ "type": "connection_established", "message": "Connected to SmartTrader" }
{ "type": "ack", "action": "subscribe", "count": 2 }
{ "type": "ticker_batch", "data": [{ "symbol": "SBIN", "ltp": 500.5, "chp": 1.2, "volume": 12345 }, ...] }
{ "type": "pong" }
```

### 4.4 WebSocket Subscription Rules (All Pages)

These rules apply universally. No exceptions.

1. Subscribe when `isConnected` becomes `true` AND symbol set is non-empty
2. Re-subscribe only when the symbol set genuinely changes — not on reconnect events
3. Unsubscribe old set before subscribing new set (on universe/symbol change)
4. Unsubscribe only on component unmount — never in the cleanup of a connection-state effect
5. The server automatically cleans up per-connection subscriptions when a WebSocket closes — do not rely on cleanup functions for server-side state

### 4.5 Market Hours Degradation (All Pages)

| Condition | Required Behaviour |
|---|---|
| Market open + Fyers connected | Full real-time data |
| Market open + Fyers disconnected | Show last known price with "Delayed" label. Auto-reconnect silently. |
| Market closed | Show closed state. Switch to yfinance global snapshot on Dashboard. |
| Fyers token expired | Show clear error state with message. Do not show stale prices as live. |
| `DEV_MODE=True` | Treat as always market-open. Bypass token validation warnings. |

### 4.6 `HistoricalPrice` Table — Pre-computed Technical Indicators

Stored per symbol per trading day. Computed during nightly data ingestion, not at query time.

| Column | Indicator |
|---|---|
| `rsi_14` | RSI 14-period |
| `ema_20` | EMA 20 |
| `ema_50` | EMA 50 |
| `macd` | MACD line |
| `macd_signal` | MACD signal line |
| `adx` | ADX 14-period |
| `stoch_k` | Stochastic %K |
| `stoch_d` | Stochastic %D |
| `bb_upper` | Bollinger Band Upper |
| `bb_middle` | Bollinger Band Middle |
| `bb_lower` | Bollinger Band Lower |
| `atr_14` | ATR 14-period |
| `trend_7d` | 7-day price trend % |
| `trend_30d` | 30-day price trend % |
| `is_breakout` | Boolean: near 20-day high |

---

## 5. Module Requirements

---

### 5.1 Dashboard

#### Purpose
At-a-glance market view during trading hours using Fyers live data. Post-market, switches to global snapshot via yfinance. First impression for any investor — must look polished and load real data instantly.

#### Behaviour: Market Hours (09:15–15:30 IST)

All data comes from Fyers. No widget shows data that isn't real.

| Widget | Data Source | Update Mechanism |
|---|---|---|
| **Top Gainers** (Nifty 50) | Fyers WebSocket ticks | Real-time via WebSocket |
| **Top Losers** (Nifty 50) | Fyers WebSocket ticks | Real-time via WebSocket |
| **Nifty 50 live price + change** | Fyers WebSocket ticks (`NSE:NIFTY50-INDEX`) | Real-time |
| **BankNifty live price + change** | Fyers WebSocket ticks (`NSE:NIFTYBANK-INDEX`) | Real-time |
| **PCR (Put-Call Ratio)** | Computed from Fyers options chain REST API | Polled every 60s |
| **Max Pain** | Computed from Fyers options chain REST API | Polled every 60s |
| **Sector Performance** | Aggregated from Fyers WebSocket ticks | Real-time |
| **Watchlist** | Fyers WebSocket ticks | Real-time |
| **Portfolio P&L** | Fyers positions REST API | Polled every 30s |
| **Indian Markets bar** | Fyers WebSocket ticks (major indices) | Real-time |

**PCR and Max Pain computation:**
These are not sourced from any external vendor. They are calculated server-side from the Fyers options chain endpoint for the nearest Nifty and BankNifty expiry:
- PCR = Total Put OI / Total Call OI (summed across all strikes for nearest expiry)
- Max Pain = the strike price at which total open option buyer losses are maximised (iterate all strikes, sum intrinsic value of all options at that strike, find minimum total loss point)
- Expose via `GET /api/market/options-sentiment?symbol=NIFTY` endpoint

#### Behaviour: Post-Market Hours

| Widget | Data Source | Refresh |
|---|---|---|
| **Global Indices** (S&P 500, Nasdaq, Dow Jones, Nifty 50, BankNifty) | yfinance | On load + manual refresh button |
| **Commodities** (Gold, Silver) | yfinance futures (`GC=F`, `SI=F`) | On load + manual refresh |
| **India VIX** | yfinance (`^INDIAVIX`) | On load |
| **US Fear & Greed** | CNN API | On load |
| **India Market Mood Index** | Tickertape scrape (VIX fallback if scrape fails) | On load |

#### Dashboard UI Requirements
- LIVE badge (green, animated) when WebSocket is connected and market is open
- Market status label: shows IST time and open/closed state
- Top Gainers / Losers: symbol, LTP, change%, sector — sorted by change%
- Sector Performance: sector name + aggregate change% — colour-coded green/red
- Watchlist: user-defined list of symbols, persisted in browser localStorage
- Portfolio: Total value (₹), Day P&L (₹ and %), Total Return (₹ and %)
- All monetary values in ₹. Volumes in L/Cr Indian notation.
- Graceful empty states: if PCR/Max Pain fetch fails, show "Unavailable" — not 0 or a dash that looks like data

---

### 5.2 Screener

#### Purpose
Enable fast discovery of trading opportunities across 33 NSE index universes with real-time price updates. Must demonstrate the data pipeline's breadth and speed.

#### Phase 1 Scope
- Equity only (no F&O OI scans)
- 33 index universes from CSV files via `IndexUniverseLoader`
- Technical indicators from pre-computed `HistoricalPrice` table (previous day's close — correct by design)
- Real-time price, change%, and volume updates via WebSocket during market hours

#### Data Per Stock Row

| Field | Source | Live Updated |
|---|---|---|
| Symbol | DB | No |
| Company Name | DB | No |
| Sector | DB | No |
| Price (LTP) | Fyers WebSocket tick | Yes — real-time |
| Change % | Fyers WebSocket tick | Yes — real-time |
| Volume | Fyers WebSocket tick | Yes — real-time |
| Market Cap | DB (`Company.market_cap`) | No |
| RSI (14) | `HistoricalPrice.rsi_14` | No — prev day |
| EMA 20 / 50 | `HistoricalPrice.ema_20/50` | No |
| MACD | `HistoricalPrice.macd` | No |
| ADX | `HistoricalPrice.adx` | No |
| ATR | `HistoricalPrice.atr_14` | No |

Technical indicators are intentionally previous day's — do not attempt to recalculate intraday.

#### Screener Features — Phase 1
- Universe selector dropdown (all 33 available indices from `GET /api/screener/indices`)
- Symbol or company name search with 300ms debounce
- Sortable columns: symbol, price, change%, volume, market cap, RSI, MACD — **global sort** across the full universe, not just visible page
- Pagination: 25 / 50 / 100 rows per page selectable
- LIVE badge when WebSocket is connected

#### Data Loading Sequence
1. Universe selected → `GET /api/screener/results?universe=nifty50` → backend returns DB technical data + Fyers REST quotes as initial price/volume values
2. After data loads → subscribe all universe symbols via WebSocket (`NSE:SBIN-EQ` format)
3. WebSocket ticks update price, change%, volume in real-time
4. Sort applied client-side across all records. Pagination applied after sort.
5. On universe change → unsubscribe old symbols → fetch new → subscribe new

#### Screener Subscription Scale
- Nifty 50: ~50 symbols
- Nifty 500: ~500 symbols
- Large universes (500 symbols) are valid — the backend and Fyers SDK support this

---

### 5.3 Terminal

#### Purpose
Live and paper trading interface for discretionary order placement on NSE Equity, F&O, and Index Futures/Options.

#### Phase 1 Scope — Simple and Robust
Phase 1 establishes the core trading workflow. Options chain, Greeks, strategy builder, and payoff diagrams are Phase 2.

#### Phase 1 Components

**Chart Panel**
- Candlestick chart for the selected symbol
- Timeframes: 1m, 5m, 15m, 1h, 1D
- EMA 20 and EMA 50 overlaid
- Historical candles from Fyers REST history endpoint
- Current candle updates live via WebSocket tick during market hours
- Instrument selector: Equity, Futures, Options (strike + expiry picker for F&O)

**Order Panel**
- Buy / Sell toggle
- Order types: Market, Limit, Stop-Loss
- Quantity input
- Price input (enabled for Limit and SL, disabled for Market)
- Paper Trade toggle — clearly and persistently labelled ("PAPER MODE" banner)
- Order review confirmation before submission
- Live orders sent to Fyers. Paper orders stored in local DB table, not sent to Fyers.

**Positions Panel**
- Open positions: symbol, instrument type, qty, avg price, LTP, unrealised P&L, P&L%
- LTP updates live via WebSocket
- Realised P&L for the day shown separately
- Paper positions shown separately from live positions with clear labelling

**Order Book Panel**
- Today's orders: symbol, type, qty, price, status (Pending / Executed / Cancelled / Rejected), time
- Sourced from Fyers orderbook REST endpoint
- Auto-refreshed every 15s

**Symbol Search**
- Type symbol or company name → get suggestions from symbol master
- On selection: load chart, subscribe WebSocket for that symbol and its open position symbols

#### Paper Trading Requirements
- Paper orders stored in a dedicated `paper_orders` DB table — never sent to Fyers
- Paper P&L calculated from live Fyers ticks, not from Fyers positions API
- Paper mode state persisted across page refreshes
- Paper mode and live mode are always visually distinct — no ambiguity ever

#### WebSocket Subscription (Terminal)
- Subscribe to: currently viewed symbol + all open position symbols (live and paper)
- On symbol change: unsubscribe previous symbol, subscribe new symbol
- Format: Fyers format (`NSE:SBIN-EQ`, `NSE:NIFTY25JANFUT` for futures, etc.)

---

### 5.4 Backtest

#### Purpose
Demonstrate a survivorship-bias-free backtesting engine using clean historical data. For Phase 1, the priority is the **data pipeline** — building a verified, corporate-action-adjusted historical database for Nifty 50. The UI is secondary to the data quality.

#### Phase 1 Scope — Data Pipeline First

**Objective:** Build and validate a clean historical OHLCV database for Nifty 50 constituents covering 2025 to present. This pipeline is the foundation for all future backtesting.

**Data Sources Available:**
- Nifty 50 constituent weightage PDFs (Jan 2025 onward) — parsed to determine which stocks were in the index on any given date
- Corporate actions Excel files — splits, bonuses, mergers, name changes, delistings
- Daily bhavcopy files — EOD price/volume data
- `SymbolLifecycleService` — already implemented, resolves symbols across rename/merger events

**Pipeline Steps — Phase 1:**
1. Parse Nifty 50 constituent PDFs → build a dated membership table: which symbols were in the index on which dates
2. Load daily bhavcopy data for all Nifty 50 constituents (2025 to date)
3. Apply corporate actions from Excel files → produce adjusted OHLCV prices
4. Use `SymbolLifecycleService` to resolve any symbol changes in the period
5. Store in DB with a `is_nifty50_constituent` date-range table
6. Validate: run a simple buy-and-hold test on Nifty 50 constituents and verify the equity curve approximately matches Nifty 50 index returns as a sanity check

**Once Phase 1 pipeline is stable** → extend scripts to additional universes and longer historical timelines. The scripts must be parameterised for this.

#### Phase 1 UI Requirements

The backtest page must not be empty or show "Coming Soon." It should show the pipeline status and allow running a basic strategy:

**Strategy:** Simple moving average crossover (EMA 20 crosses EMA 50) on Nifty 50 constituents as of each simulation date (survivorship-bias-aware).

**Backtest Parameters (UI inputs):**
- Universe: Nifty 50 (only, in Phase 1)
- Date range: within available data (2025 to present)
- Strategy: EMA crossover (fixed in Phase 1 — no custom strategy builder yet)

**Backtest Results (UI outputs):**
- Equity curve chart (portfolio value over time) with drawdown shaded below
- Benchmark overlay: Nifty 50 index buy-and-hold return for the same period
- Key metrics: Total Return %, CAGR, Max Drawdown %, Sharpe Ratio, number of trades
- Trade log table: symbol, entry date, entry price, exit date, exit price, P&L, P&L%

**No dummy results.** If the data pipeline has not been run, the page shows a clear state: "Historical data not yet loaded. Run the data pipeline to enable backtesting." with a button to trigger the pipeline run (or instructions if it must be run manually).

---

## 6. API Contracts

### REST Endpoints

| Method | Path | Module | Description |
|---|---|---|---|
| `GET` | `/api/screener/indices` | Screener | List all 33 index universes |
| `GET` | `/api/screener/results` | Screener | Stocks with technical indicators + initial prices |
| `GET` | `/api/websocket/status` | All | Fyers WS + market status |
| `POST` | `/api/websocket/connect` | All | Trigger Fyers WS connection |
| `POST` | `/api/websocket/subscribe` | All | REST-based symbol subscription |
| `POST` | `/api/websocket/disconnect` | All | Disconnect Fyers WS |
| `GET` | `/api/system/health` | All | Token validity + DB + WS status |
| `GET` | `/api/market/global` | Dashboard | Global indices via yfinance |
| `GET` | `/api/market/sentiment` | Dashboard | Fear & Greed + India MMI |
| `GET` | `/api/market/condition` | Dashboard | Nifty ADX-based trend analysis |
| `GET` | `/api/market/options-sentiment` | Dashboard | PCR + Max Pain (computed from Fyers) |
| `GET` | `/api/terminal/chart` | Terminal | Historical OHLCV candles from Fyers |
| `GET` | `/api/terminal/positions` | Terminal | Live positions from Fyers |
| `GET` | `/api/terminal/orders` | Terminal | Order book from Fyers |
| `POST` | `/api/terminal/order` | Terminal | Place live order via Fyers |
| `GET` | `/api/terminal/paper/positions` | Terminal | Paper positions from local DB |
| `POST` | `/api/terminal/paper/order` | Terminal | Place paper order (stored locally) |
| `GET` | `/api/backtest/status` | Backtest | Data pipeline status + available date range |
| `POST` | `/api/backtest/run` | Backtest | Run backtest, returns job ID |
| `GET` | `/api/backtest/result/{job_id}` | Backtest | Poll result or stream progress |

### WebSocket Endpoint
```
ws://localhost:8000/api/websocket/stream
```

---

## 7. UI / UX Principles

### Design Standard
The system should look like it belongs in a trading desk environment. Dark theme throughout. Clean, dense data display. No decorative elements that don't carry information.

### Component Rules
- Use existing `GlassCard` and `PageContainer` components from `@/components/ui` for consistency
- Monetary values: always ₹, never bare numbers for prices
- Volume: L (Lakh) / Cr (Crore) Indian notation
- Market cap: L Cr / K Cr notation
- Change values: colour-coded — green for positive, red for negative — consistently everywhere
- Timestamps: IST always

### Connection Status
A persistent WebSocket status indicator must be visible on Dashboard, Screener, and Terminal at all times. It must reflect the true state — never show CONNECTED when the socket is not open.

### Loading States
Every data-loading operation must show a skeleton or spinner. No blank sections. The `loading.tsx` skeleton pattern already exists in the screener — use it as the standard.

### Empty States
Empty states must explain why they are empty and what the user can do:
- "Market is closed. Data shown is from last close." (not a blank chart)
- "Fyers token has expired. Please refresh your token." (not a spinner that spins forever)
- "No positions open." (not a blank table)
- "Historical data not loaded. Run the data pipeline to enable backtesting." (not a blank page)

### Responsiveness
Desktop-first. v1 is not required to be mobile responsive.

### Notifications
No toast spam for connection events. Toasts only for user-initiated actions (order placed successfully, order failed with reason).

---

## 8. Environment Configuration

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `DEV_MODE` | No | `true` bypasses market hours and token validation |
| `NEXT_PUBLIC_WS_URL` | No | Override frontend WebSocket URL |
| `NEXT_PUBLIC_API_URL` | No | Override frontend API base URL (default: `http://localhost:8000`) |
| `FYERS_TOKEN_FILE` | No | Path to access token JSON (default: `fyers/config/access_token.json`) |

---

## 9. Explicit Out-of-Scope for Phase 1

The following are documented so agents do not accidentally implement them:

- Multi-user authentication or session management
- Options chain UI with bid/ask/OI table
- Greeks display (Delta, Theta, Gamma, Vega, IV)
- Multi-leg strategy builder (spreads, straddles, iron condor)
- Payoff diagram
- OI-based screener scans (Put-Call OI buildup)
- Custom screener filter builder (user-defined criteria)
- Screener alerts / notifications
- Backtest on any universe other than Nifty 50
- Backtest date range before 2025
- Walk-forward optimisation
- Automated / algo order execution
- MCX Commodities trading
- Mobile / PWA
- Portfolio analytics beyond day P&L and total return
- Any external paid data vendor or API

---

## 10. Phase Roadmap

### Phase 1 — Stable Foundation (2–3 months)
| Module | Deliverable |
|---|---|
| Dashboard | Real-time Nifty 50 gainers/losers, index ticks, PCR/Max Pain from Fyers, yfinance post-market view |
| Screener | 33 universes, real-time price updates, technical indicators from DB, global sort and pagination |
| Terminal | Chart with EMA overlay, basic equity + F&O order placement (live + paper), positions panel, order book |
| Backtest | Nifty 50 data pipeline clean and verified, EMA crossover strategy, equity curve + drawdown + trade log + benchmark |

### Phase 2 — Feature Depth (Post Phase 1)
| Module | Additions |
|---|---|
| Dashboard | Sector heatmap visual, portfolio return attribution |
| Screener | OI buildup/unwinding scans, custom filter builder, screener alerts |
| Terminal | Options chain with live bid/ask/OI, Greeks, multi-leg strategy builder, payoff diagram |
| Backtest | Additional universes (Nifty 100, 500, sector indices), extended historical timeline, walk-forward optimisation |

---

## 11. Glossary

| Term | Definition |
|---|---|
| DB format | Short uppercase symbol: `SBIN` |
| Fyers format | Provider-prefixed: `NSE:SBIN-EQ` |
| Tick | A single real-time price update from Fyers for one symbol |
| Ticker batch | Collection of ticks flushed as one WebSocket message every 500ms |
| Universe | Named set of stocks (e.g. Nifty 50 = 50 specific symbols at a point in time) |
| Constituent-aware | Backtest only includes stocks that were index members on the simulation date |
| Survivorship bias | Error of analysing only currently listed stocks, ignoring de-listed ones |
| Corporate action | Split, bonus, merger, name change, or delisting affecting historical prices |
| Adjusted price | Historical price corrected for corporate actions (splits, bonuses) |
| PCR | Put-Call Ratio: Total Put OI / Total Call OI for a given expiry |
| Max Pain | Strike price at which total option buyer losses are maximised |
| Paper trade | Simulated trade stored locally — no real order sent to Fyers |
| DEV_MODE | Env flag: bypass market hours + token validation for development |
| IST | Indian Standard Time, UTC+5:30 — all market timing uses this |
| Phase 1 | Simple and robust — current development target (2–3 months) |
| Phase 2 | Feature expansion — begins only after Phase 1 is stable |
