---
status: ready
priority: p1
issue_id: "001"
tags: [phase0, cleanup, governance]
dependencies: []
---

# Problem Statement
Legacy planning/tooling artifacts and drifted implementation patterns block a clean PRD-v2 execution baseline.

# Findings
- `.kiro/` and `.trae/` are present despite reset decision.
- Existing codebase includes legacy/mock/out-of-scope patterns.
- Execution docs and todo tracker were not yet formalized for long-running phased delivery.

# Proposed Solutions
## A (recommended)
Delete `.kiro/` and `.trae/`, establish `docs/execution/*`, and initialize `todos/`.

## B
Keep old directories and only add new docs.

# Recommended Action
Execute option A.

# Acceptance Criteria
- [ ] `.kiro/` removed
- [ ] `.trae/` removed
- [ ] `docs/execution/` contains master plan, decision log, risk register, baseline audit
- [ ] `todos/` initialized with phase slices and dependencies

# Work Log
### 2026-02-19 - Initialization
- Created execution docs and initialized todo system.
- Pending: directory cleanup and verification.
