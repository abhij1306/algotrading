# MASTER IMPLEMENTATION PROMPT — SmartTrader v3.0

## Role
You are a Principal Quant Systems Architect refactoring an existing platform into an institutional-grade, research-driven, risk-governed trading system.

**This system must mirror how professional quant funds operate.**

---

## CORE PHILOSOPHY (NON-NEGOTIABLE)

1. **Research explains risk — not returns**
2. **Governance controls permission — not performance**
3. **Monitoring observes — never configures**
4. **No UI element may exist without a database-backed reason**
5. **No mock, placeholder, or synthetic data is allowed anywhere**

### Data Integrity Rule
If required data does not exist:
- Backend returns `204` or `404`
- Frontend shows a blocking empty state
- No charts, tables, or inferred metrics render

---

## NAVIGATION (FINAL — DO NOT ADD SCREENS)

```
Research
  ├─ Strategy Research
  └─ Portfolio Research

Governance
  ├─ Strategies
  ├─ Universes
  └─ Portfolio Policy

Monitoring  (DEFAULT LANDING)

Settings
```

---

## MODULE DEFINITIONS (STRICT)

### 1️⃣ Research

#### Strategy Research
**Question answered:** "Does this strategy deserve to exist?"

**Data sources:**
- `strategy_contracts`
- `backtest_daily_results`

**Render rules:**
- If no backtest data → blocking empty state

**Outputs:**
- Equity curve
- Drawdown curve
- Drawdown forensics table (stored once, immutable)
- "WHEN IT LOSES" text (mandatory)

**Controls:**
- Approve → Governance
- Reject → Archive
- No parameter tuning. No re-running.

#### Portfolio Research
**Question answered:** "How do approved strategies behave together?"

**Inputs:**
- Date range (from DB bounds only)
- Approved strategies only
- Portfolio policy (selection only)

**Outputs:**
- Portfolio equity
- Portfolio drawdown
- Strategy contribution table
- Correlation matrix

**Writes:**
- `research_portfolios`
- `backtest_runs`
- `backtest_daily_results`

**Controls:**
- Save Research Portfolio
- Promote to Live

---

### 2️⃣ Governance

#### Strategies
- Lifecycle only: `RESEARCH` → `LIVE` → `RETIRED`
- Show frozen risk profile
- Show "WHEN IT LOSES"
- No charts, no performance ranking

#### Universes
- Asset eligibility definition only
- Liquidity & rebalance rules
- Read-only compatibility list

#### Portfolio Policy
- Allocation method selection
- Risk knobs only:
  - Allocator sensitivity
  - Correlation penalty
  - Drawdown defense
- All changes logged. No strategy-level controls.

---

### 3️⃣ Monitoring (Read-only)

**Renders ONLY if** `portfolio_daily_state` has rows

**Shows:**
- System state
- Portfolio equity & DD
- Strategy trust map
- Governance actions log

**Forbidden:**
- No backtests
- No approvals
- No configuration

---

## DATABASE IS THE SOURCE OF TRUTH

> **No API may compute what is not stored.**  
> **No UI may infer missing data.**

---

## PHASE GATES (MANDATORY)

### Phase 1
- Navigation + empty states only
- No charts
- No data calls

### Phase 2
- Database schema
- API returns 204/404 correctly

### Phase 3
- Research fully functional
- Only then enable Monitoring

---

## FINAL RULE

If anything is unclear:
1. **STOP**
2. **ASK**
3. **DO NOT INVENT**

> This system must fail safely before it looks impressive.

---

## ADD-ON: STRATEGY EXPANSION FRAMEWORK

### Objective
Extend the system with additional strategies that improve regime coverage and diversification, while fully respecting:
- Research → Governance → Monitoring flow
- No optimization
- No parameter tuning in UI
- Immutable risk profiles once approved
- Database-first, UI-second rule

**The goal is coverage, not performance ranking.**

---

### 1️⃣ STRATEGY ADDITION PHILOSOPHY (MANDATORY)

**Core Principle:** Strategies are risk hypotheses, not alpha widgets.

Each strategy must:
- Represent a distinct market behavior
- Fail in clearly explainable regimes
- Add uncorrelated return streams, not incremental Sharpe

#### ❌ Forbidden
- Strategy variants with only timeframe or threshold differences
- Parameter grids
- "Improved" versions of the same logic
- ML / optimization / adaptive tuning

#### ✅ Required
- Each strategy must justify its existence via regime differentiation

---

### 2️⃣ STRATEGY CATEGORIES TO ADD (APPROVED SET)

You may add only the following strategy types. Each must be implemented as a separate `strategy_contract` with locked parameters.

#### A. Trend / Momentum (Directional)

**1. Index Trend Following (Daily)**
- Asset: NIFTY / BANKNIFTY
- Logic: Long-only when price > long-term trend filter
- Holding: Multi-day
- Regime: Sustained directional markets
- **WHEN IT LOSES:** "Choppy, range-bound markets with frequent trend reversals."

**2. Time-Series Momentum (Cross-Asset)**
- Assets: Index futures or ETFs
- Logic: Lookback-based direction (fixed window)
- Holding: 1–5 days
- Regime: Macro expansion / contraction
- **WHEN IT LOSES:** "Sudden regime flips and volatility compression."

#### B. Mean Reversion

**3. Intraday Index Mean Reversion**
- Asset: Index
- Logic: Fade extreme intraday deviations
- Holding: Same day
- Regime: Sideways / range markets
- **WHEN IT LOSES:** "Strong trend days and breakout regimes."

**4. Overnight Gap Reversion**
- Asset: Index
- Logic: Fade statistically extreme overnight gaps
- Holding: Open → Midday
- Regime: News-driven overreactions
- **WHEN IT LOSES:** "True information gaps with continuation."

#### C. Volatility / Risk Regime

**5. Volatility Expansion Breakout**
- Asset: Index
- Logic: Enter on volatility expansion after compression
- Holding: 1–3 days
- Regime: Transition from calm → stress
- **WHEN IT LOSES:** "False breakouts and low follow-through environments."

**6. Volatility Mean Reversion**
- Asset: Index
- Logic: Short volatility after extreme spikes
- Holding: Short duration
- Regime: Panic exhaustion
- **WHEN IT LOSES:** "Crisis cascades and multi-day volatility expansion."

#### D. Defensive / Crisis

**7. Crash Protection Overlay**
- Asset: Index
- Logic: Tail-risk hedge activation
- Holding: Event-based
- Regime: Market stress
- **WHEN IT LOSES:** "Slow drawdowns without volatility expansion."

---

### 3️⃣ STRATEGY CONTRACT REQUIREMENTS (STRICT)

Every new strategy MUST define the following at creation time:

```json
{
  "strategy_id": "string",
  "name": "string",
  "regime_tag": "Momentum | MeanReversion | Volatility | Defensive",
  "asset_class": "Index",
  "timeframe": "LOCKED",
  "holding_period": "string",
  "parameters": "LOCKED",
  "when_it_loses": "string (mandatory)",
  "lifecycle_state": "RESEARCH"
}
```

❌ **No strategy may be added without `when_it_loses`.**

---

### 4️⃣ BACKTEST & STORAGE RULES

#### Backtest Execution
Backtests are triggered only via:
- Research → Portfolio Research

Strategy-level backtests exist only to compute risk metrics

#### Stored Outputs
For each strategy, persist:
- Daily returns
- Equity curve
- Drawdown curve
- Drawdown forensics:
  - Max DD
  - 95th percentile DD
  - DD duration
  - Recovery time

**Once stored and approved:**
- ❌ Never recomputed
- ❌ Never overwritten
- ❌ Never re-optimized

---

### 5️⃣ UI INTEGRATION (NO NEW SCREENS)

#### Strategy Research
- New strategies automatically appear in Strategy Library
- Must show:
  - Regime tag
  - Lifecycle state
  - "WHEN IT LOSES"
- Charts render only if backtest data exists

#### Portfolio Research
- New strategies selectable only after approval
- Used only for correlation and portfolio-level behavior

#### Governance
- Strategy lifecycle controls apply unchanged
- No special handling for new strategies

---

### 6️⃣ SYSTEM-LEVEL VALIDATION (MANDATORY)

Before marking this phase complete, verify:
- ✅ Each strategy occupies a distinct regime
- ✅ At least one strategy loses money in every market condition
- ✅ Portfolio Research shows correlation reduction
- ✅ No two strategies differ only by timeframe or thresholds

---

### 7️⃣ FINAL CONSTRAINT

If a proposed strategy:
- Cannot explain when it loses
- Cannot be placed in a distinct regime
- Exists only to "improve returns"

👉 **DO NOT IMPLEMENT IT**
