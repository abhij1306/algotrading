---
status: complete
priority: p1
issue_id: "005"
tags: [screener, universe, data-integrity]
dependencies: ["004"]
---

# Problem Statement
Screener must load 33 universes with DB technicals + initial live quote hydration and no out-of-scope filters.

# Findings
- Core universe loading exists, but contract and scope require strict revalidation.

# Proposed Solutions
## A (recommended)
Lock screener row contract and remove non-Phase-1 behavior.

# Recommended Action
Execute after dashboard slices.

# Acceptance Criteria
- [x] 33 universes load from index loader
- [x] Empty/error states explicit
- [x] Non-PRD filters/features removed

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after Dashboard D1-D3 completion.
- Refactored backend screener router to canonical Phase-1 surface only:
  - kept `/api/screener/indices`
  - kept `/api/screener/results`
  - removed legacy root screener endpoint and non-Phase-1 filter/preset complexity from active contract
- Added strict universe validation and deterministic row contract from DB technicals + initial live quote hydration.
- Hardened frontend screener index load path with explicit universe-loading and error states (no silent failures).
- Verified loader universe count: `33` (from `IndexUniverseLoader.INDEX_FILES`).
- Updated endpoint verification script to canonical screener endpoints.
- Verification run:
  - `npx eslint app/screener/page.tsx` ✅
  - `python -m py_compile backend/app/routers/screener.py` ✅
  - `npm run build` ✅
