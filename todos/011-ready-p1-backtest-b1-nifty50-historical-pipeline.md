---
status: ready
priority: p1
issue_id: "011"
tags: [backtest, pipeline, nifty50, corporate-actions]
dependencies: ["010"]
---

# Problem Statement
Backtest Phase-1 depends on a reproducible constituent-aware Nifty50 historical data pipeline.

# Findings
- PRD demands 2025+ timeline with corporate action adjustments and lifecycle reconciliation.

# Proposed Solutions
## A (recommended)
Implement auditable pipeline stages with validation artifacts.

# Recommended Action
Execute after terminal completion.

# Acceptance Criteria
- [ ] Constituent timeline generated from PDFs
- [ ] Bhavcopy ingestion works for target period
- [ ] Corporate action adjustments applied
- [ ] Symbol lifecycle reconciliation done
- [ ] Pipeline reproducibility report generated

# Work Log
### 2026-02-19
- Task seeded from master plan.
- Unblocked after Terminal T1-T3 completion.
