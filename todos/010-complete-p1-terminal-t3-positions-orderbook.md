---
status: complete
priority: p1
issue_id: "010"
tags: [terminal, positions, orderbook, websocket]
dependencies: ["009"]
---

# Problem Statement
Positions and order book panels must be trustworthy, updated, and clearly separated for live/paper states.

# Findings
- High risk of mixed labels and stale values without strict contract validation.

# Proposed Solutions
## A (recommended)
Build separate data paths for live/paper positions and unified render contract with explicit labels.

# Recommended Action
Execute after T2 completion.

# Acceptance Criteria
- [x] Positions live updates via websocket
- [x] Orderbook polling and rendering works
- [x] No ambiguity between paper and live rows

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after T2 completion.
- Added positions/orderbook data panel integration in Terminal:
  - polling live positions and orders from trading APIs
  - explicit paper/live order separation in rendered rows
- Added websocket tick application to live positions for near-real-time LTP/P&L updates.
- Maintained explicit mode labels to avoid paper/live ambiguity.
- Verification run:
  - `npx eslint app/terminal/page.tsx` ✅
  - `npm run build` ✅ (`/terminal` first load JS: 119 kB)
