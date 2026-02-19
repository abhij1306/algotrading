---
status: pending
priority: p1
issue_id: "013"
tags: [backtest, validation, benchmark, metrics]
dependencies: ["012"]
---

# Problem Statement
Backtest results must be credible with equity curve, drawdown, trade log, and benchmark sanity checks.

# Findings
- Phase-1 requires investor-grade validation surface, not just raw output.

# Proposed Solutions
## A (recommended)
Implement deterministic metrics pipeline and benchmark overlay checks.

# Recommended Action
Pending triage after B2.

# Acceptance Criteria
- [ ] Equity curve and drawdown render correctly
- [ ] Trade log metrics are internally consistent
- [ ] Benchmark overlay and sanity checks pass

# Work Log
### 2026-02-19
- Task seeded from master plan.
