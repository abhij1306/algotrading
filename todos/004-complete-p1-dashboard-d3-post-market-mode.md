---
status: complete
priority: p1
issue_id: "004"
tags: [dashboard, post-market, yfinance, sentiment]
dependencies: ["003"]
---

# Problem Statement
Post-market dashboard mode must work without Fyers live feed and clearly label snapshot sources/staleness.

# Findings
- Current market data endpoints overlap and need canonical behavior alignment.

# Proposed Solutions
## A (recommended)
Normalize post-market data flow via canonical endpoints and explicit source labels.

# Recommended Action
Execute after D2 completion.

# Acceptance Criteria
- [x] Global indices/commodities/VIX render in post-market mode
- [x] Fear & Greed + India MMI fallback behavior implemented
- [x] Data source and staleness clearly indicated

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after D2 completion.
- Post-market panel now uses canonical `/api/market/overview` data flow (global indices, VIX, sentiment, condition, timestamp).
- Added explicit sentiment cards for US Fear & Greed and India MMI, including source display for fallback visibility.
- Added snapshot timestamp and source labels in post-market widgets.
- Removed synthetic neutral fallback defaults in backend sentiment service (`Unavailable` when source data unavailable).
- Verification run:
  - `npx eslint components/dashboard/InsightsPanel.tsx components/dashboard/PostMarketInsights.tsx app/dashboard/page.tsx components/dashboard/MarketHoursInsights.tsx` ✅
  - `python -m py_compile backend/app/services/market_data_service.py backend/app/routers/market.py` ✅
