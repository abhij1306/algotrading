---
status: complete
priority: p1
issue_id: "008"
tags: [terminal, chart, ohlcv, websocket]
dependencies: ["007"]
---

# Problem Statement
Terminal needs a reliable Phase-1 chart/instrument context with live updates and real source data only.

# Findings
- Current terminal surface appears partial and requires PRD contract alignment.

# Proposed Solutions
## A (recommended)
Build a strict chart data contract and live-tick integration for selected instrument context.

# Recommended Action
Execute after screener completion.

# Acceptance Criteria
- [x] OHLCV chart loads for selected symbol/instrument
- [x] EMA20/EMA50 overlays present
- [x] Timeframe switching works
- [x] Live candle/tick updates during market hours

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after Screener S1-S3 completion.
- Added canonical terminal chart API: `GET /api/terminal/chart` with symbol/timeframe/limit support.
- Integrated real chart rendering in Terminal with OHLCV+EMA20/EMA50 and live tick updates on selected symbol.
- Timeframe switching wired to backend chart fetch path.
- Added code-split chart component to keep terminal first-load bundle within target.
- Verification run:
  - `python -m py_compile backend/app/routers/terminal.py backend/app/main.py backend/app/routers/__init__.py` ✅
  - `npx eslint app/terminal/page.tsx components/terminal/PriceChart.tsx` ✅
  - `npm run build` ✅ (`/terminal` first load JS: 118 kB)
