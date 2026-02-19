---
status: complete
priority: p1
issue_id: "003"
tags: [dashboard, widgets, phase1]
dependencies: ["002"]
---

# Problem Statement
Dashboard widgets must be constrained to PRD Phase-1 and sourced only from approved data providers.

# Findings
- Mixed/legacy widgets and non-PRD surfaces may exist.

# Proposed Solutions
## A (recommended)
Keep only PRD Phase-1 widgets and remove out-of-scope UI/data paths.

# Recommended Action
Execute after D1 completion.

# Acceptance Criteria
- [x] Top gainers/losers + sector + watchlist + portfolio P&L comply with PRD
- [x] No hardcoded/placeholder runtime values
- [x] Out-of-scope widgets removed from Dashboard

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after D1 completion.
- Removed fake zero-placeholder behavior from backend `/api/market/indices`; endpoint now returns only valid live rows or empty list.
- Normalized dashboard index mapping for `BANKNIFTY`/`NIFTY IT` so live updates are routed correctly.
- Added explicit source labels (`Fyers`) on market-hours insights widgets.
- Removed implicit watchlist synthetic values by returning nullable values from backend and rendering explicit `—`/`Unavailable` on dashboard.
- Removed portfolio `₹0.00` fallback when stats are unavailable.
- Verification run:
  - `npx eslint app/dashboard/page.tsx components/dashboard/MarketHoursInsights.tsx` ✅
  - `python -m py_compile backend/app/routers/market.py` ✅
