---
status: complete
priority: p1
issue_id: "006"
tags: [screener, websocket, symbol-master]
dependencies: ["005"]
---

# Problem Statement
Screener live update model must follow shared subscription lifecycle and symbol boundary rules.

# Findings
- Subscription churn and symbol-format drift are recurring risk areas.

# Proposed Solutions
## A (recommended)
Apply shared subscription contract and strict symbol conversion at API boundaries.

# Recommended Action
Execute after S1 completion.

# Acceptance Criteria
- [x] Subscribe/unsubscribe lifecycle follows PRD rules
- [x] Live updates only for price/change/volume
- [x] Fyers<->DB symbol mapping verified via `symbol_master`

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after S1 completion.
- Fixed aggregate websocket subscription lifecycle in backend:
  - live provider subscribe/unsubscribe now operates on net symbol deltas across all connected clients.
  - disconnect cleanup now unsubscribes only symbols no longer needed by any client.
- Hardened `LiveMarketService` queue consistency:
  - dedupe subscribe symbols
  - remove unsubscribed symbols from pending queue
- Verified symbol boundary normalization remains enforced at websocket API boundary via `symbol_master.to_db` and provider boundary via `symbol_master.batch_to_fyers`.
- Verification run:
  - `python -m py_compile backend/app/routers/websocket.py backend/app/services/live_market_service.py backend/app/utils/ws_manager.py` ✅
  - `npx eslint app/screener/page.tsx hooks/useWebSocket.ts` ✅
