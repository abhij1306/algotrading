---
status: complete
priority: p1
issue_id: "002"
tags: [dashboard, websocket, market-status]
dependencies: ["001"]
---

# Problem Statement
Dashboard must satisfy PRD D1 with truthful LIVE state, IST market status, and robust degraded handling.

# Findings
- Existing dashboard has mixed logic and potential drift in live/offline truthfulness.

# Proposed Solutions
## A (recommended)
Rebuild D1 with explicit state machine: open+connected, open+disconnected, closed.

# Recommended Action
Implement D1 as first dashboard slice.

# Acceptance Criteria
- [x] LIVE badge only when socket open and market open
- [x] Delayed/offline state visible when disconnected during open market
- [x] Closed state blocks false live indications
- [x] WebSocket reconnect behavior verified

# Work Log
### 2026-02-19
- Task created from PRD slice plan.
- Dashboard now fetches authoritative market status from `/api/market/status`.
- Header status state machine enforced:
  - open + socket connected => `LIVE`
  - open + socket disconnected => `DELAYED`
  - closed => `CLOSED`
- Removed frontend-local market-open truth dependency for dashboard state decisions.
- Verification run:
  - `npx eslint app/dashboard/page.tsx` ✅
  - `npm run build` ✅
  - `npm run lint` ❌ (pre-existing unrelated repo issues)
