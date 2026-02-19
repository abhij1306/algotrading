---
status: complete
priority: p1
issue_id: "009"
tags: [terminal, orders, paper-trading]
dependencies: ["008"]
---

# Problem Statement
Order panel must provide strict live/paper separation with clear UX and correct broker boundaries.

# Findings
- Phase-1 requires explicit no-ambiguity paper mode behavior.

# Proposed Solutions
## A (recommended)
Enforce separate execution paths and persistent paper mode state with visible banner.

# Recommended Action
Execute after T1 completion.

# Acceptance Criteria
- [x] Market/limit/SL order forms function
- [x] Paper mode persists across refreshes
- [x] Paper orders never hit broker API
- [x] Live orders routed only through Fyers paths

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after T1 completion.
- Added persistent terminal trading mode (`PAPER`/`LIVE`) with explicit mode banner and mode toggle controls.
- Implemented order form for `MARKET`/`LIMIT`/`SL` with quantity/price/trigger inputs and buy/sell execution.
- Added strict paper execution path: `POST /api/terminal/paper/order` writes paper order records directly and bypasses broker path.
- Live execution path remains routed through existing live order APIs (`/api/trading/mode`, `/api/trading/order`).
- Verification run:
  - `python -m py_compile backend/app/routers/terminal.py` ✅
  - `npx eslint app/terminal/page.tsx components/terminal/PriceChart.tsx` ✅
  - `npm run build` ✅ (`/terminal` first load JS: 119 kB)
