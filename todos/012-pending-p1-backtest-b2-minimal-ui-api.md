---
status: pending
priority: p1
issue_id: "012"
tags: [backtest, api, ui, phase1]
dependencies: ["011"]
---

# Problem Statement
Backtest page must expose minimal runnable Phase-1 flow with explicit empty state when data is unavailable.

# Findings
- Existing backtest flow includes mock/runtime drift risks.

# Proposed Solutions
## A (recommended)
Remove mock runtime path and enforce real run/status/result flow.

# Recommended Action
Pending triage after B1.

# Acceptance Criteria
- [ ] Backtest run/status/result endpoints aligned with PRD
- [ ] Frontend shows actionable empty state when data missing
- [ ] No “coming soon” or mock runtime output

# Work Log
### 2026-02-19
- Task seeded from master plan.
