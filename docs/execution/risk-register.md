# SmartTrader Risk Register (Phase-1 Rebuild)

## R-001: API Namespace Drift
- Severity: High
- Description: duplicate/legacy endpoints across routers create behavior inconsistency.
- Mitigation: freeze canonical Phase-1 endpoints and remove/alias only intentionally.
- Owner: Backend slice owners
- Status: Open

## R-002: WebSocket Subscription Regressions
- Severity: High
- Description: page-level subscription churn can cause missed ticks or leak subscriptions.
- Mitigation: enforce shared lifecycle rules + add targeted websocket tests per module.
- Owner: Frontend + WebSocket core
- Status: Open

## R-003: Hidden Dummy/Mock Paths
- Severity: High
- Description: mock/data fallback code may produce non-real values in Phase-1 pages.
- Mitigation: inventory and delete/disable mock paths in touched slices.
- Owner: Module owners
- Status: Open

## R-004: Backtest Data Integrity
- Severity: Critical
- Description: constituent history/corporate actions inaccuracies can invalidate outcomes.
- Mitigation: build auditable ingestion pipeline with reconciliation and validation report.
- Owner: Backtest/data pipeline
- Status: Open

## R-005: Large Existing Diff Context
- Severity: Medium
- Description: repository already has many unrelated modifications.
- Mitigation: confine changes to tracked slice scope and avoid reverting unrelated edits.
- Owner: All contributors
- Status: Open
