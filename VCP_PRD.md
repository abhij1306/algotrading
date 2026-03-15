# SmartTrader — VCP Strategy Module PRD (Final)
# Appendix to SMARTTRADER_PRD_v2.md

> **Version:** 2.0 — Supersedes VCP PRD v1.0
> **Module:** Strategies Page (5th page)
> **Scope:** VCP — automated live trading + backtesting
> **Methodology:** Minervini VCP + RS Rating filter + market regime awareness
> **Last Updated:** 2026-03-15

---

## 1. Module Purpose and Design Philosophy

The Strategies page runs the VCP strategy in two modes:

1. **Automated Live Trading** — EOD scan identifies signals, system places orders at next
   morning's open, manages positions autonomously throughout the day
2. **Backtest** — tests the identical logic on historical data with survivorship-bias-aware
   universe membership

### Core Design Principles

**Every rule must be unambiguous.** Automated systems fail at edge cases. Every parameter,
every condition, every order has a single defined behaviour. There are no "it depends" rules.

**High probability over high frequency.** The filter stack (Stage 2 → RS Rating → VCP
structure → Volume → Overhead resistance → Market regime) is designed to pass 5–15
signals per week in a bull market. Every filter removed increases signal count and
decreases win rate. Do not loosen filters to generate more trades.

**The backtest and live system use identical logic.** If the backtest uses next-day open
entry, the live system uses next-day open entry. If the backtest uses 21-EMA trail, the
live system uses 21-EMA trail. Any divergence between backtest logic and live logic makes
the backtest results meaningless.

**Paper trade first.** Before enabling live order placement, run the system in paper mode
for a minimum of 20 trading days to verify order flow, position tracking, and P&L
reconciliation against Fyers actual data.

---

## 2. Complete Signal Filter Stack

Filters are applied in this exact sequence. A stock that fails any filter is immediately
excluded — no further analysis is performed on it. Sequence is ordered from cheapest
(DB query) to most expensive (multi-day OHLCV analysis) to minimise compute.

```
500 stocks (Nifty 500)
    │
    ▼ Filter 1: Stage 2 Uptrend (DB query — fast)
~150 survivors
    │
    ▼ Filter 2: Relative Strength Rating ≥ 80
~80 survivors
    │
    ▼ Filter 3: VCP Base Structure (15–60 days, max 30% depth)
~40 survivors
    │
    ▼ Filter 4: Contraction Sequence (2–4 contractions, each tighter)
~25 survivors
    │
    ▼ Filter 5: Volume Dry-up (final contraction vol < 40% of 50d avg)
~15 survivors
    │
    ▼ Filter 6: Breakout Trigger (price > pivot + vol surge)
~5–10 signals
    │
    ▼ Filter 7: Overhead Resistance (no supply within 3% above pivot)
    │
    ▼ Filter 8: Market Regime (full size vs half size)
    │
    ▼ Filter 9: Gap-up Check (skip if open > 2% above pivot)
    │
    ▼ FINAL SIGNALS (A and B grade only, C hidden by default)
```

---

## 3. Filter Specifications

### Filter 1 — Stage 2 Uptrend Prerequisite

All five conditions must be true. Evaluated using the most recent available EOD data.

| # | Condition | Rule |
|---|---|---|
| 1 | Price vs long-term MAs | Close > 150-day MA AND Close > 200-day MA |
| 2 | MA alignment | 150-day MA > 200-day MA |
| 3 | MA direction | 200-day MA today > 200-day MA 20 trading days ago |
| 4 | Proximity to 52W high | Close ≥ 75% of 52-week high (within 25% of high) |
| 5 | Full MA stack | 50-day MA > 150-day MA > 200-day MA |

**Parameter:** `stage2_ma_lookback` default 200 days, tunable 150–250.
If any single condition fails → stock excluded immediately.

### Filter 2 — Relative Strength Rating

RS Rating measures a stock's price performance relative to all other stocks in the
universe over the past 52 weeks. It is calculated entirely from price data — no
external data source required.

**Calculation:**
```
RS_Score = (
    0.40 × return_3_months +
    0.20 × return_6_months +
    0.20 × return_9_months +
    0.20 × return_12_months
)

RS_Rating = percentile rank of RS_Score within the universe (0–99)
```

This is the Investor's Business Daily methodology adapted for internal calculation.
The weights favour recent performance (40% to last 3 months) while incorporating
the full year.

**Thresholds:**

| Grade | RS Rating Requirement | Rationale |
|---|---|---|
| A-grade signal | RS ≥ 90 | Top 10% of universe — genuine leaders |
| B-grade signal | RS ≥ 80 | Top 20% — still strong but not elite |
| Excluded | RS < 80 | Laggards — VCP breakouts fail disproportionately |

**Parameter:** `rs_rating_min` default 80, tunable 70–95.
**Parameter:** `rs_rating_a_grade_min` default 90, tunable 80–99.

**Implementation note:** RS Rating must be computed for all stocks in the universe
before scanning begins (single pass over price data). Store in `vcp_scan_results`
per scan. Do not recompute per-stock — compute once for the full universe, then
use as a lookup.

### Filter 3 — Base Structure

The VCP base is the consolidation structure containing the contractions.

| Parameter | Default | Tunable Range | Rule |
|---|---|---|---|
| `base_min_days` | 15 | 10–30 | Base must span at least this many trading days |
| `base_max_days` | 60 | 30–90 | Bases older than this are stale |
| `base_max_depth` | 30% | 15–40% | Max decline from pivot high to lowest base low |

Base start = the highest closing price in the past `base_max_days` days.
Base low = the lowest closing price since the base start.
Base depth = (Base High − Base Low) / Base High × 100.

### Filter 4 — Contraction Sequence

A contraction is a pivot high → pivot low swing within the base.

**Pivot detection algorithm:**
A local high is a candle whose high is higher than the N candles on each side.
A local low is a candle whose low is lower than the N candles on each side.
Use N=3 (tunable: 2–5). This avoids noise while capturing meaningful swings.

| Parameter | Default | Tunable Range | Description |
|---|---|---|---|
| `pivot_detection_window` | 3 | 2–5 | N candles on each side for pivot detection |
| `min_contractions` | 2 | 2–4 | Minimum contractions required |
| `max_contractions` | 4 | 2–5 | Maximum (more = extended, stale base) |
| `first_contraction_max_depth` | 25% | 15–35% | First contraction max depth |
| `second_contraction_max_depth` | 15% | 8–25% | Second contraction max depth |
| `final_contraction_max_depth` | 10% | 3–15% | Final contraction max depth |
| `contraction_shrink_required` | True | True/False | Each must be strictly tighter than previous |

**A-grade final contraction threshold:** ≤ 5% depth. Signals meeting this are flagged
as A-grade regardless of other factors (assuming all other filters pass).

If `contraction_shrink_required = True` and any contraction is wider than the previous
→ pattern invalid, stock excluded.

### Filter 5 — Volume Dry-up

Volume must confirm that selling pressure is exhausting through the contractions.

| Parameter | Default | Tunable Range | Description |
|---|---|---|---|
| `volume_ma_period` | 50 | 20–65 | Baseline average volume period |
| `final_contraction_vol_max` | 40% | 20–60% | Volume in final contraction vs avg |
| `volume_trend_required` | True | True/False | Declining trend required across base |

**Volume trend check:** Linear regression slope across all volume bars within the base.
Slope must be negative. If `volume_trend_required = True` and slope is positive → excluded.

**Final contraction check:** Average daily volume during the final contraction period
must be ≤ `final_contraction_vol_max` × 50-day average volume.

### Filter 6 — Breakout Trigger

Evaluated on the most recent completed trading day (EOD scan) or intraday
(real-time breakout promotion during market hours).

All three conditions must be simultaneously true:

| Condition | Rule | Parameter |
|---|---|---|
| Price | Close > Pivot High | — (Pivot High = highest close before final contraction) |
| Volume | Today's volume ≥ N × 50-day average volume | `breakout_volume_multiplier` default 1.5, range 1.25–2.5 |
| Confirmation | Close is within top 25% of the day's range (not a reversal candle) | — |

The close-position check (within top 25% of range) prevents false breakouts where
price briefly exceeded the pivot but sold off by close — a distribution signal, not
accumulation.

```
Range = Day High − Day Low
Close Position = (Close − Day Low) / Range
Required: Close Position ≥ 0.75
```

### Filter 7 — Overhead Resistance Check

Scans 52-week price history for prior supply zones above the current pivot.

**Resistance zone definition:** Price spent ≥ 5 consecutive trading days within a
2% price band at a given level within the past 252 trading days.

**Algorithm:**
```
Use a sliding-window check over observed close levels L in the range
(pivot_high, pivot_high × (1 + buffer)):
    count = number of consecutive days where abs(close - L) / L <= 0.02
    if count >= 5:
        RESISTANCE FOUND → exclude stock
```

**Granularity:** L is evaluated from observed close levels in the historical series,
not from an arbitrary continuous loop. This preserves the documented 2% band while
anchoring the check to actual traded price levels.

**Parameter:** `overhead_resistance_buffer` default 3%, tunable 1–5%.

If any resistance zone exists within `overhead_resistance_buffer`% above the pivot
→ stock excluded. No exceptions.

### Filter 8 — Market Regime (Position Sizing Modifier)

This filter does not exclude stocks — it modifies position sizing based on the
health of the broader market.

| Nifty 50 Condition | Risk Per Trade | Rationale |
|---|---|---|
| Close > 200-day MA (bull market) | 1.0% of capital | Full size — market is supportive |
| Close < 200-day MA (bear market) | 0.5% of capital | Half size — higher failure rate |

**Evaluation:** Checked once per day at scan time using Nifty 50 (`NSE:NIFTY50-INDEX`)
EOD data. The regime status is stored with each scan result.

**Behaviour during regime transition:**
- Existing open positions are NOT reduced when regime switches from bull to bear.
  Position sizing reduction applies only to NEW entries.
- When regime switches back to bull, new entries resume full 1% sizing.
- Regime status is displayed prominently in the UI at all times.

### Filter 9 — Gap-up Check (Live Order Only, Not Backtest)

Applied at order placement time (09:15 IST the morning after a signal is generated).

| Scenario | Action | Reason |
|---|---|---|
| Open ≤ Pivot + 2% | Place market order at open | Clean entry, within acceptable range |
| Open > Pivot + 2% but ≤ Pivot + 5% | Skip entry, log as "Gap-up skip" | Extended entry, risk/reward degraded |
| Open > Pivot + 5% | Skip entry, log as "Large gap-up skip" | Too extended, stop would need to move |
| Open below Pivot | Do not enter, keep on watch list | Breakout not confirmed at open |

**Parameter:** `max_gap_up_pct` default 2%, tunable 1–5%.

**Backtest treatment of gap-ups:**
In the backtest, when a signal triggers and the next day's open is above pivot + `max_gap_up_pct`,
the trade is skipped. This ensures backtest results accurately reflect what the live
system would have done.

---

## 4. Signal Grading

| Grade | All conditions required |
|---|---|
| **A** | RS ≥ 90, final contraction ≤ 5%, volume ≤ 40% of avg, breakout volume ≥ (4/3 × `breakout_volume_multiplier`) × avg when breakout occurs, close in top 25% of range |
| **B** | RS ≥ 80, final contraction 5–10%, volume ≤ 60% of avg, breakout volume ≥ `breakout_volume_multiplier` × avg when breakout occurs |
| **C** | Meets minimum criteria, one quality factor borderline |

Default display: A and B only. C signals hidden unless "Show all" enabled.

---

## 5. Risk Management — Complete Specification

### 5.1 Position Sizing

```
Market Regime:
  Bull (Nifty > 200d MA): risk_pct = 1.0%
  Bear (Nifty < 200d MA): risk_pct = 0.5%

stop_offset_pct = configurable percentage offset applied below Final Contraction Low
  default example: 0.1%

Stop Loss Price = Final Contraction Low × (1 − stop_offset_pct)

Entry Price = Next day's opening price (market order)
  → At backtest time: use actual next-day open from historical data
  → At live time: use actual Fyers fill price

Stop Distance (₹) = Entry Price − Stop Loss Price
Stop Distance (%) = Stop Distance / Entry Price × 100

Capital at Risk = Total Capital × risk_pct

Shares = FLOOR(Capital at Risk / Stop Distance)

Validation:
  If Entry Price < Stop Loss Price:
    Reject trade and log "Invalid stop distance"
  If Stop Distance <= 0:
    Reject trade and log "Invalid stop distance"
  If Shares <= 0:
    Reject trade and log "Invalid stop distance"

Position Value = Shares × Entry Price

Hard Cap Check:
  If Position Value > Total Capital × 0.20:
    Shares = FLOOR(Total Capital × 0.20 / Entry Price)
    → Actual risk becomes less than risk_pct in this case
    → Log the cap override: "Position capped at 20% of capital"
```

**Why 20% cap:** No position limit means in theory 100% of capital could go into one
name. The 20% hard cap prevents any single position from dominating the portfolio
regardless of how tight the stop is.

### 5.2 Entry — Market Order at Open

**Timing:** Order placed at market open (09:15 IST) the trading day after the EOD
signal is generated.

**Order type:** Market order. No limit, no stop-limit. The system accepts whatever
the opening price is, subject to the gap-up check (Filter 9).

**Sequence:**
```
15:45 IST (after market close):
  Run EOD scan → identify breakout signals
  Store signals in vcp_scan_results with status = 'PENDING_ENTRY'
  Calculate: stop price, shares, position value for each signal
  Log: "Signal generated for SYMBOL. Planned entry tomorrow at open."

09:00 IST next morning (pre-market):
  For each PENDING_ENTRY signal:
    Check if signal is still valid:
      → Is Nifty 50 open (market is open)?
      → Has the stock already been entered (duplicate check)?

09:15 IST (market open):
  For each valid PENDING_ENTRY signal:
    Fetch opening price from Fyers
    Apply gap-up check (Filter 9)
    If gap-up check passes:
      Place market order via Fyers API
      Record actual fill price
      Create position record with:
        entry_price = actual fill
        stop_price = final_contraction_low × (1 − stop_offset_pct)
        shares = as calculated (may differ slightly due to fill vs planned)
        two_r_price = entry + (2 × (entry − stop))
        status = OPEN
    If gap-up check fails:
      Update signal status = 'SKIPPED_GAP_UP'
      Log reason
```

If actual entry_price < stop_price, or if computed Stop Distance <= 0, or if
Shares <= 0 after applying `risk_pct`, reject the trade and log
"Invalid stop distance" instead of creating a position.

### 5.3 Stop Loss Management

**Stop price** is fixed at entry. It does not move until either:
- 2R is hit → stop moves to breakeven (entry price)
- Position is closed

**Stop monitoring:**
```
Every 5 minutes during market hours (09:15–15:25 IST):
  For each OPEN position:
    Fetch latest tick from Fyers WebSocket
    If current LTP ≤ stop_price:
      Place market sell order for ALL remaining shares
      Mark position status = 'STOPPED_OUT'
      Log: "SYMBOL stopped out at LTP. Loss = X₹ (Y% of capital)"

15:25 IST (5 minutes before close):
  No new stop checks — allow last 5 minutes to settle
  Final position marks done at 15:30 EOD price
```

**Why market order on stop, not stop-limit:** Stop-limit orders can fail to fill
in fast markets. A market sell guarantees exit. The 0.1% slippage assumption in
the backtest accounts for this.

### 5.4 Partial Exit at 2R

```
Every 5 minutes during market hours:
  For each OPEN position where 2R not yet hit:
    If current LTP ≥ two_r_price:
     If current LTP ≥ two_r_price:
       Place market sell order for CEIL(shares / 2)   ← sell exactly half, round up
       If shares == 1:
           Mark position status = 'CLOSED_AT_2R'
           Log: "SYMBOL 1-share position fully closed at 2R."
       Else:
           Update position:
             shares_remaining = shares − sold
             stop_price = entry_price  (move stop to breakeven)
           two_r_hit = True        two_r_hit = True
        two_r_hit_time = now
        two_r_hit_price = actual fill price
      Log: "SYMBOL hit 2R. Sold half at X₹. Stop moved to breakeven."
```

**Edge case — 2R and stop hit same day:**
For daily-bar backtesting, use deterministic precedence:
- If daily_low ≤ stop_price: treat stop as hit first and exit all remaining shares at stop.
- Else if daily_high ≥ two_r_price and daily_low > stop_price: treat 2R as hit first,
  take the partial exit at 2R, and move the stop to breakeven for the remainder.

### 5.5 Trail Stop — 21-day EMA

After 2R is hit, the remaining position is managed with a 21-day EMA trail.

```
EOD check at 15:35 IST every trading day:
  For each position where two_r_hit = True:
    Calculate 21-day EMA using today's close
    If today's close < 21-day EMA:
      Place market sell order at next morning's open for ALL remaining shares
      Mark position status = 'TRAIL_STOP_EXIT'
      Log: "SYMBOL trail stop triggered. Close X₹ < 21-EMA Y₹."
```

**Why EOD check, not intraday:** The 21-EMA trail is a swing trading exit — it's
designed to keep you in the position through normal intraday volatility while
exiting if the multi-day trend breaks. Checking intraday would cause premature
exits on normal pullback days.

**Trail stop exit order timing:** Market order at next morning's open (same
mechanism as entry). Not a same-day close order.

### 5.6 Position Lifecycle State Machine

```
PENDING_ENTRY
    │ 09:15 open — gap check passes → market order placed
    ▼
OPEN
    │ LTP ≤ stop_price → market sell all
    ├──────────────────────────────► STOPPED_OUT (full loss, ~1% of capital)
    │
    │ LTP ≥ two_r_price → market sell half, stop → breakeven
    ▼
OPEN_PARTIAL (half position remaining, stop at breakeven)
    │ LTP ≤ entry_price → market sell remainder
    ├──────────────────────────────► BREAKEVEN_EXIT (0R on remainder, +2R on half)
    │
    │ EOD close < 21-day EMA → market sell remainder at next open
    ▼
TRAIL_STOP_EXIT (positive, amount depends on how far price ran)

SKIPPED_GAP_UP — signal generated but entry skipped (gap too large)
SKIPPED_REGIME — signal generated during bear market pause (not applicable — we still enter at half size)
```

---

## 6. Automation Architecture

### 6.1 Scheduler

New service: `backend/app/services/strategy_scheduler.py`

Runs as a background task within the FastAPI application lifecycle.

```
Daily Schedule:

09:00 IST  Pre-market check
           → Validate Fyers token
           → Confirm market is open (check NSE holiday calendar)
           → Fetch list of PENDING_ENTRY signals from DB
           → For each: calculate gap-up check using pre-market / futures price if available
           → Log: "X signals pending entry today"

09:15 IST  Open orders
           → For each valid PENDING_ENTRY signal:
               Apply gap-up check using actual opening price
               Place market order if check passes
               Record fill

09:20–15:25  Position monitoring (every 5 minutes)
           → Check all OPEN positions against stop_price
           → Check all OPEN positions (pre-2R) against two_r_price
           → Place exit orders immediately if triggered

15:35 IST  EOD processing
           → Check all OPEN_PARTIAL positions against 21-day EMA
           → Queue trail stop exits for next morning's open if triggered
           → Run EOD VCP scan on all universe symbols
           → Generate new PENDING_ENTRY signals for tomorrow
           → Update RS Ratings for full universe
           → Log daily summary: positions open, P&L today, new signals

15:45 IST  Reconciliation
           → Fetch actual positions from Fyers API
           → Compare against internal position records
           → Flag any discrepancies for manual review
           → Log: "Reconciliation complete. X positions matched. Y discrepancies."

09:15 next day
           → Process any TRAIL_STOP_EXIT orders queued from previous EOD
```

### 6.2 Kill Switch

A hardware kill switch accessible from the Strategies page header and via REST API.

```
UI: Large red "HALT" button always visible on Strategies page
    → Requires single click + confirmation modal (2-step to prevent accidental trigger)

Effect of HALT:
    → Set system flag: TRADING_HALTED = True
    → Cancel all pending open orders via Fyers API
    → Pause scheduler (no new orders placed)
    → Do NOT close existing open positions (they remain, manually managed)
    → Display banner: "TRADING HALTED — [timestamp] — [reason if provided]"

API: POST /api/strategies/halt
     POST /api/strategies/resume
     GET  /api/strategies/status  → returns {halted: bool, halted_at: timestamp}
```

### 6.3 Reconciliation Service

Runs at 15:45 IST daily and on-demand.

```python
def reconcile():
    """
    Compare internal position records against Fyers actual positions.
    Detect and log discrepancies.
    """
    fyers_positions = fyers_client.get_positions()
    internal_positions = db.query(Position).filter(status.in_(['OPEN', 'OPEN_PARTIAL'])).all()

    for internal in internal_positions:
        fyers_match = find_in_fyers(internal.symbol, fyers_positions)
        if not fyers_match:
            log_discrepancy(f"MISSING: {internal.symbol} in internal records but not in Fyers")
        elif abs(fyers_match.qty - internal.shares_remaining) > 0:
            log_discrepancy(f"QTY MISMATCH: {internal.symbol} internal={internal.shares_remaining} fyers={fyers_match.qty}")

    for fyers_pos in fyers_positions:
        if not find_in_internal(fyers_pos.symbol, internal_positions):
            log_discrepancy(f"UNKNOWN: {fyers_pos.symbol} in Fyers but not in internal records")
```

Discrepancies are displayed on the Strategies page under a "Reconciliation" panel
and do NOT prevent the system from running — they are flagged for human review.

### 6.4 Position Monitoring Service

```python
class PositionMonitor:
    """
    Runs every 5 minutes during market hours.
    Subscribes to WebSocket ticks for all open position symbols.
    Processes stop and 2R checks on each tick.
    """

    async def on_tick(self, tick: dict):
        symbol = tick['symbol']
        ltp = tick['ltp']

        position = self.open_positions.get(symbol)
        if not position:
            return

        # Stop check
        if ltp <= position.stop_price:
            await self.execute_stop_exit(position, ltp)
            return

        # 2R check (only for positions not yet at 2R)
        if not position.two_r_hit and ltp >= position.two_r_price:
            await self.execute_partial_2r_exit(position, ltp)
```

**Note:** Position monitor uses the existing Fyers WebSocket infrastructure.
Open position symbols are added to the WebSocket subscription automatically
when a position is opened.

---

## 7. Live Scan UI — Final Specification

### 7.1 Page Layout

```
┌────────────────────────────────────────────────────────────────────┐
│  STRATEGIES   [VCP ▾]                              [■ HALT]         │
│                                                                      │
│  Universe: [NIFTY500 ▾]    Last scan: Today 15:45 IST              │
│  Regime: ● BULL (Nifty > 200d MA)    Risk: 1.0% per trade          │
│                                                  [▶ Run Scan Now]   │
├────────────────────────────────────────────────────────────────────┤
│  [Forming Setups  12]  [Live Breakouts  3]  [Open Positions  5]     │
│                         [Backtest]                                   │
├────────────────────────────────────────────────────────────────────┤
│  [Signal table / position table — see below]                        │
├────────────────────────────────────────────────────────────────────┤
│  [Chart panel — expands on row click]                               │
└────────────────────────────────────────────────────────────────────┘
```

**Regime indicator** must always be visible. When regime = BEAR, the indicator
turns amber and shows "BEAR (Half Size — 0.5% risk)".

**HALT button** always visible in the header. Red. Cannot be missed.

### 7.2 Tab 1 — Forming Setups

Stocks in a valid VCP base, not yet broken out. Watch list.

| Column | Description |
|---|---|
| Grade | A / B badge |
| Symbol | Ticker + name |
| RS Rating | 0–99 (green if ≥ 90, white if 80–89) |
| Sector | NSE sector |
| Contractions | Count + depths e.g. "3: 18→11→4%" |
| Final Depth | Final contraction % (tighter = better) |
| Vol Dry-up | Volume as % of 50d avg in final contraction |
| Pivot High | Price level to watch |
| Stop Level | Contraction low × (1 − `stop_offset_pct`) |
| Stop % | Stop as % of pivot high |
| Days in Base | Trading days since base started |
| Chart | Sparkline thumbnail |

Default sort: Grade A first, then by Final Depth ascending (tightest first).

### 7.3 Tab 2 — Live Breakouts

Stocks that triggered the breakout today. Actionable signals.

All Forming columns plus:

| Column | Description |
|---|---|
| Breakout Price | Today's close (the breakout candle) |
| Vol Surge | Today's volume as × of 50d avg |
| Planned Entry | Tomorrow's market open |
| Planned Stop | Final contraction low × (1 − `stop_offset_pct`) |
| Stop % | Stop distance % |
| Shares (1% risk) | Calculated shares based on current capital |
| Position Value | Shares × planned entry (estimated) |
| 2R Target | Entry + 2 × stop distance (estimated) |
| Regime | BULL / BEAR with risk size |

**"Queue for Entry" button** on each row. Clicking sets signal status =
PENDING_ENTRY for the scheduler to process at next morning's open.
Button changes to "Queued ✓" after clicking. Can be cancelled before 09:15.

### 7.4 Tab 3 — Open Positions

All currently open positions managed by the system.

| Column | Description |
|---|---|
| Symbol | Ticker |
| Entry Date | Date entered |
| Entry Price | Actual fill price |
| Shares | Current shares held |
| Stop Price | Current stop (breakeven if post-2R) |
| LTP | Live price from WebSocket |
| Unrealised P&L ₹ | (LTP − Entry) × Shares |
| Unrealised P&L % | vs entry price |
| R-Multiple | Current gain/loss in R units |
| 2R Status | NOT HIT / HIT [date] |
| Trail Stop | 21-EMA value (shown after 2R hit) |
| Status | OPEN / OPEN_PARTIAL |

**Manual Override buttons per row:**
- "Close Now" — immediately places market sell for all remaining shares
- "Adjust Stop" — allows manual stop price edit (for exceptional cases only)

### 7.5 Chart Panel (Click-to-Expand)

Opens on clicking any signal or position row.

**Chart content:**
- Daily candlestick chart, minimum 6 months
- Overlaid MAs: 50d (blue), 150d (orange), 200d (red), 21d EMA (white dashed)
- Contraction highs/lows: horizontal lines marking each pivot
- Pivot High: dashed green horizontal line labelled "PIVOT"
- Stop Loss: dashed red horizontal line labelled "STOP"
- 2R Target: dashed gold horizontal line labelled "2R TARGET"
- For open positions: entry price marked with a triangle annotation
- Volume panel: bars coloured red (above avg) / blue (below avg)

**Pattern summary alongside chart:**
```
Grade: A
RS Rating: 94
Contractions: 3 detected (19% → 11% → 4%)
Stage 2: ✓ all 5 conditions
Vol dry-up: 34% of 50d avg in final contraction
Regime: BULL → 1.0% risk

Pivot High:  ₹XXX.XX
Stop Level:  ₹XXX.XX  (X.X% below pivot)
Shares:      XXX shares
Position ₹:  ₹XX,XXX  (X.X% of capital)
Capital Risk: ₹X,XXX  (1.0% of capital)
2R Target:   ₹XXX.XX
Est. 2R P&L: ₹XX,XXX (+X.X%)
```

---

## 8. Backtest — Final Specification

### 8.1 Survivorship Bias — Practical Approach

**Phase 1 (current data pipeline covers 2025 to date):**
Use date-specific Nifty 500 constituent membership from parsed index PDFs.
Backtest date range limited to period covered by constituent data.

**Fallback for extended history (before PDF data is available):**
If user selects a date range that predates the constituent data, show a clear warning:
"Constituent data available from [earliest date]. Using current Nifty 500 list for
earlier dates introduces survivorship bias. Results marked with ⚠ are indicative only."

Do not refuse to run — show the result with the bias warning clearly labelled.
This is more useful than refusing and more honest than hiding the limitation.

### 8.2 Backtest Engine

Uses identical logic to the live system. Key parity checkpoints:

| Live System | Backtest Must Match |
|---|---|
| Entry: market order at next open | Entry: next day's actual open price from historical data |
| Gap-up check: skip if open > pivot + 2% | Same gap-up check using historical open vs prior day's pivot |
| Stop: 5-min WebSocket monitoring | Stop: if daily low ≤ stop price, treat stop as hit first |
| 2R: 5-min monitoring | 2R: only check daily high if daily low stayed above stop price |
| Trail: EOD close vs 21-EMA | Trail: same |
| Regime: Nifty 50 vs 200-day MA | Same |
| RS Rating: computed from universe | Same RS calculation on historical data |

**Slippage model:**
- Entry: actual next-day open (no additional slippage — market order at open is
  generally filled at or very near the opening price for liquid NSE stocks)
- Stop exit: stop_price − 0.1% (assumes slight adverse fill)
- 2R exit: two_r_price − 0.1%
- Trail exit: next-day open − 0.1%

### 8.3 Backtest Output

#### Summary Metrics Card
| Metric | Formula |
|---|---|
| Total Return % | (Final − Start) / Start × 100 |
| CAGR % | (Final / Start) ^ (252 / trading_days) − 1 |
| Max Drawdown % | Max peak-to-trough of equity curve |
| Sharpe Ratio | (Ann. Return − 6.5%) / Ann. Std Dev of daily returns |
| Win Rate % | Profitable trades / Total trades × 100 |
| Avg Win ₹ / Avg Loss ₹ | Averages of winning / losing trades |
| Profit Factor | Gross profit / Gross loss |
| Total Trades | All completed trades |
| Avg Hold Days | Average days from entry to any exit |
| Avg R-Multiple | Average outcome in R units (target: > 1.0) |
| Benchmark Return % | Nifty 50 buy-and-hold same period same capital |
| Alpha | Strategy CAGR − Benchmark CAGR |
| Survivorship Bias Label | "BIAS-CORRECTED" or "⚠ INDICATIVE" |

#### Equity Curve Chart
- Portfolio value over time (₹)
- Drawdown shaded red below rolling peak
- Nifty 50 benchmark overlaid (normalised to same starting capital)
- Regime periods shaded: green = bull, amber = bear

#### Trade Log Table
Every trade, sortable:

| Column | Description |
|---|---|
| Symbol | Stock |
| Grade | A / B |
| RS at Entry | RS Rating when signal was generated |
| Entry Date | Date entered |
| Entry Price | Actual open used |
| Exit Date | Final exit date |
| Exit Price | Final exit price |
| Exit Reason | STOPPED / 2R+TRAIL / TRAIL_STOP |
| Shares | Shares held |
| P&L ₹ | Total profit/loss |
| P&L % | Return on position |
| R-Multiple | P&L / Initial risk |
| Hold Days | Calendar days |
| Regime | BULL/BEAR at entry |

Clicking any trade row → opens chart with entry/exit annotations.

### 8.4 Methodology Disclosure

A collapsible "Methodology" section on the backtest results page states:

```
Entry: Market order at next-day open. Gap-up threshold: [X]%.
       Trades skipped due to gap-up: [N] of [total signals].
Stop:  Fixed at final contraction low × (1 − stop_offset_pct). Triggered using daily low.
       Exit price = stop price − 0.1% slippage.
2R:    Triggered using daily high only if daily low stayed above stop.
       Exit price = 2R price − 0.1% slippage.
Trail: EOD close vs 21-day EMA. Exit at next-day open − 0.1% slippage.
Regime: Nifty 50 vs 200-day MA. Bear market = 0.5% risk per trade.
Universe: [NIFTY500 — BIAS CORRECTED using date-specific constituents]
          OR [NIFTY500 — ⚠ INDICATIVE — uses current constituents for pre-[date] data]
Transaction costs: Not modelled (add-on Phase 2).
RS Rating: Computed internally using 3/6/9/12-month weighted return percentile.
```

---

## 9. Database Schema Additions

### `vcp_scan_results`
```sql
id                       SERIAL PRIMARY KEY
scan_id                  UUID / VARCHAR(36) NOT NULL
scan_date                DATE NOT NULL
scan_timestamp           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
universe                 VARCHAR(50) NOT NULL
symbol                   VARCHAR(20) NOT NULL
grade                    CHAR(1) NOT NULL
rs_rating                INTEGER NOT NULL
stage2_conditions_met    INTEGER NOT NULL          -- 0 to 5
contraction_count        INTEGER NOT NULL
contraction_depths       JSONB NOT NULL            -- [18.2, 11.4, 4.1]
final_contraction_depth  DECIMAL(5,2) NOT NULL
volume_dry_up_pct        DECIMAL(5,2) NOT NULL
pivot_high               DECIMAL(12,2) NOT NULL
stop_level               DECIMAL(12,2) NOT NULL
stop_pct                 DECIMAL(5,2) NOT NULL
days_in_base             INTEGER
is_breakout              BOOLEAN DEFAULT FALSE
breakout_price           DECIMAL(12,2)
breakout_volume_mult     DECIMAL(5,2)
close_position_in_range  DECIMAL(5,2)              -- 0.0 to 1.0
overhead_clear           BOOLEAN NOT NULL
regime                   VARCHAR(10) NOT NULL      -- 'BULL' or 'BEAR'
signal_status            VARCHAR(30) DEFAULT 'SIGNAL'
                         -- SIGNAL / PENDING_ENTRY / ENTERED / SKIPPED_GAP_UP
created_at               TIMESTAMP DEFAULT NOW()
```

### `positions`
```sql
id                  SERIAL PRIMARY KEY
symbol              VARCHAR(20) NOT NULL
scan_result_id      INTEGER REFERENCES vcp_scan_results(id)
entry_date          DATE NOT NULL
entry_price         DECIMAL(12,2) NOT NULL
stop_price          DECIMAL(12,2) NOT NULL
shares              INTEGER NOT NULL
shares_remaining    INTEGER NOT NULL
two_r_price         DECIMAL(12,2) NOT NULL
two_r_hit           BOOLEAN DEFAULT FALSE
two_r_hit_date      DATE
two_r_hit_price     DECIMAL(12,2)
regime_at_entry     VARCHAR(10) NOT NULL
risk_pct_at_entry   DECIMAL(4,2) NOT NULL
status              VARCHAR(30) NOT NULL
                    -- OPEN / OPEN_PARTIAL / STOPPED_OUT / TRAIL_STOP_EXIT
                    -- BREAKEVEN_EXIT / MANUALLY_CLOSED
exit_date           DATE
exit_price          DECIMAL(12,2)
exit_reason         VARCHAR(30)
pnl_inr             DECIMAL(12,2)
pnl_pct             DECIMAL(8,2)
r_multiple          DECIMAL(6,2)
is_paper            BOOLEAN DEFAULT FALSE
created_at          TIMESTAMP DEFAULT NOW()
updated_at          TIMESTAMP DEFAULT NOW()
```

### `backtest_runs` and `backtest_trades`
VCP backtests write into the shared `backtest_runs` / `backtest_trades` lane so
run lifecycle, persistence, and history remain aligned with the broader
backtesting framework.

### Integration notes
- `vcp_scan_results.signal_status` carries `SKIPPED_GAP_UP`; skipped entries do not create `positions` rows.
- `positions.scan_result_id` links live positions back to the originating VCP scan result.
- Future Quant Research / Portfolio integration should add explicit hooks such as
  `backtest_run_id` on VCP scan artifacts and `portfolio_daily_result_id` (or an
  equivalent portfolio FK) on positions instead of duplicating universe/symbol logic.

### `system_config`
```sql
key    VARCHAR(100) PRIMARY KEY
value  TEXT NOT NULL
-- Entries:
-- 'TRADING_HALTED': 'true' / 'false'
-- 'HALT_REASON': text
-- 'DEFAULT_CAPITAL': '1000000'
-- 'DEFAULT_UNIVERSE': 'NIFTY500'
-- 'MAX_GAP_UP_PCT': '2.0'
-- 'BREAKOUT_VOLUME_MULT': '1.5'
-- etc.
```

---

## 10. New API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/strategies/vcp/scan/latest` | Latest cached scan results |
| `POST` | `/api/strategies/vcp/scan/run` | Trigger new on-demand scan |
| `GET` | `/api/strategies/vcp/signal/{symbol}` | Full pattern detail + chart data |
| `POST` | `/api/strategies/vcp/signal/{id}/queue` | Queue signal for next-day entry |
| `POST` | `/api/strategies/vcp/signal/{id}/cancel` | Cancel queued entry |
| `GET` | `/api/strategies/positions` | All open positions with live P&L |
| `POST` | `/api/strategies/positions/{id}/close` | Manual close — places market sell |
| `POST` | `/api/strategies/positions/{id}/stop` | Adjust stop price manually |
| `GET` | `/api/strategies/regime` | Current market regime + Nifty 50 vs 200MA |
| `POST` | `/api/strategies/halt` | Activate kill switch |
| `POST` | `/api/strategies/resume` | Deactivate kill switch |
| `GET` | `/api/strategies/status` | Scheduler status, halt state, reconciliation |
| `POST` | `/api/strategies/vcp/backtest/run` | Start backtest, returns run_id |
| `GET` | `/api/strategies/vcp/backtest/{run_id}` | Poll status + results |
| `GET` | `/api/strategies/vcp/backtest/{run_id}/trades` | Trade log |
| `GET` | `/api/strategies/vcp/backtest/history` | Previous runs list |

---

## 11. Phase Roadmap

### Phase 1 — Automated VCP (current)
Live scan, Forming/Breakouts/Positions tabs, automated order placement,
5-minute position monitoring, EOD trail check, kill switch, reconciliation,
backtest with survivorship-bias handling.

### Phase 2 — Extensions
- Transaction costs in backtest (STT + brokerage)
- Additional patterns (Cup & Handle, Flat Base)
- Intraday breakout detection (not just EOD)
- Walk-forward parameter optimisation
- Screener alerts (notify when Forming setup approaches breakout)
- Multi-strategy portfolio backtest
- Trade export to CSV

---

## 12. Pre-Launch Checklist (Paper Mode First)

Before enabling live order placement:

- [ ] Paper mode runs for minimum 20 trading days without errors
- [ ] Reconciliation shows zero discrepancies for 5 consecutive days
- [ ] Kill switch tested: activates, cancels pending orders, displays banner
- [ ] Gap-up skip logic verified against at least 3 known gap-up scenarios
- [ ] 2R partial exit fires correctly when price hits 2R level
- [ ] Stop exit fires correctly when price hits stop level
- [ ] Trail stop fires correctly at EOD when close < 21-EMA
- [ ] Regime switch tested: scan run in both bull and bear conditions
- [ ] Backtest result on 2025 data verified manually for at least 5 trades
- [ ] Fyers API order placement tested with 1-share test orders
