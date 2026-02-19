# Stitch Prompt: SmartTrader Backtest UI (Design-System Strict)

Use this prompt in Stitch to generate HTML for SmartTrader Backtest screens.
Goal: produce clean, compact layouts that match existing SmartTrader UI patterns, then map 1:1 into current React components.

## Prompt to Feed Stitch

You are designing **two pages** for SmartTrader:
1. **Backtest Builder** (`/backtest`) - single-page run setup and recent runs
2. **Backtest Results** (`/backtest/results/{runId}`) - run output only

### Hard Constraints
- Use **existing SmartTrader design system only**.
- Do **not** introduce new font families, color systems, spacing scales, radii, shadows, or component styles.
- Keep layout **compact and dense** (similar to Dashboard/Screener/Terminal).
- No oversized hero sections, no excessive vertical whitespace.
- Desktop-first, responsive down to tablet/mobile without redesigning component language.

### Design System Rules (must follow)
- Typography:
  - Single app typography stack from SmartTrader (already defined globally).
  - Numeric values use mono/tabular style only where needed (metrics, prices, percentages, run IDs).
- Tokens:
  - Use semantic token-driven styling (background/surface/border/foreground/profit/loss/warning).
- Component language:
  - Card, CardHeader, CardContent, Button, Input, Select, Badge, Table, Tabs, Empty/Error states.
  - Keep controls visually consistent with existing form controls in app.

### Page 1: Backtest Builder (`/backtest`)

#### Header Row (compact)
- Left: title `Backtest` only (no subtitle)
- Right: small status badges
  - `Data Ready` / `Data Not Ready`
  - `Options Enabled` / `Options Blocked`

#### Section A: Run Configuration (single card)
Fields:
- Run Name (optional text)
- Instrument (select): `Equity`, `Options`
- Initial Capital (number)
- Start Date (date)
- End Date (date)
- Selection Mode (select): `Index Universe (index price only)`, `Specific Symbols`
- Conditional field:
  - If universe mode: Universe select (NIFTY50, BANKNIFTY, and available universes)
  - If symbols mode: comma-separated symbols input

Layout:
- Tight 2-3 column grid on desktop, stacked on mobile
- Uniform control heights, compact label spacing

#### Section B: Strategy Portfolio (inside same card or adjacent compact card)
Rows for strategies:
- Checkbox enable/disable
- Strategy name
- Weight input
- Strategy ID badge

Default strategies shown:
- EMA20/EMA50 Crossover
- 2-Day Momentum
- 3-Day Mean Reversion

Footer row:
- Left: `Selected strategies: N`
- Right: primary CTA `Run Backtest`

#### Section C: Recent Runs (table card)
Columns:
- Run ID
- Status (badge: running/completed/failed)
- Instrument
- Date Range
- Scope (Universe/Symbols)
- Action (`View`)

Behavioral states to visualize:
- Empty: `No runs yet`
- Loading skeleton style
- Inline error strip (non-blocking)

### Page 2: Backtest Results (`/backtest/results/{runId}`)

#### Header Row
- Title: `Backtest Result`
- Subtext: run ID
- Actions:
  - `New Run` (to `/backtest`)
  - `Back` (to `/backtest`)

#### Meta Strip
- Status badge
- Instrument
- Range
- Selection scope

#### KPI Row (compact cards)
- Total Return %
- Final Equity
- Sharpe
- Max Drawdown %
- Trades
- Win Rate %

#### Charts
- Card 1: `Equity Curve vs Benchmark`
- Card 2: `Drawdown Curve`
- Compact chart containers (no tall empty area)

#### Trade Log Table
Columns:
- Symbol
- Entry Date
- Exit Date
- Entry Px
- Exit Px
- Return %

States:
- No trades message
- Failed run error card
- Running state card (`Backtest executing...`)

### UX Details to Enforce
- Preserve visual rhythm from existing pages:
  - small section gaps
  - compact card paddings
  - no giant margins
- Keep button hierarchy clear:
  - one dominant primary CTA per page area
- Always show deterministic empty/loading/error states.

### Output Format Required from Stitch
Return:
1. Semantic HTML structure for both pages.
2. Class naming aligned with token-based utility style (SmartTrader style naming).
3. Clear section comments so it can be converted directly into:
   - `frontend/app/backtest/page.tsx`
   - `frontend/app/backtest/results/[runId]/page.tsx`
4. Do not include custom fonts or external style libraries.

## Component Mapping Notes (for conversion after Stitch)
- Header/status strip -> `Card` or bordered container + `Badge`
- Form controls -> `Input` + native/select component wrappers
- Strategy rows -> compact grid rows inside bordered block
- Recent runs + trade log -> `Table` primitives
- Result metrics -> small `Card` tiles
- Charts -> existing chart containers (Recharts wrappers in app)
