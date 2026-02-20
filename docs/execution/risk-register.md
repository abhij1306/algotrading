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

## R-006: External Source Path Drift
- Severity: High
- Description: external monthly weight source files outside canonical repo layout can change unexpectedly.
- Mitigation: materialize required source files into `data_system/01_sources/nse_index_weights_pdf` with checksums and manifest.
- Owner: Data pipeline
- Status: Mitigated

## R-007: NIFTY50 Monthly Constituents Inconsistency
- Severity: High
- Description: some monthly sources can produce non-50 constituent snapshots.
- Mitigation: enforce anomaly report + validation warning + fallback chain with source priority tagging.
- Owner: Data pipeline
- Status: Open

## R-008: Legacy Script Reactivation
- Severity: Medium
- Description: old data scripts can be used accidentally and reintroduce stale outputs.
- Mitigation: document `phase1_build.py` as canonical entrypoint and maintain archive candidate inventory.
- Owner: Maintainers
- Status: Mitigated

## R-009: Legacy Data Shadowing Canonical Artifacts
- Severity: High
- Description: stale legacy folders can be read accidentally by scripts/services and create inconsistent outputs.
- Mitigation: archive-first relocation of non-canonical trees under `archive/data-legacy/2026-02-19/`, retain only active canonical/runtime folders in `data_system/`.
- Owner: Data pipeline
- Status: Mitigated

## R-010: Bhavcopy Coverage Gaps for Phase-1 Window
- Severity: High
- Description: canonical price ingestion now depends only on bhavcopy files; missing daily bhavcopy files produce snapshot price gaps.
- Mitigation: enforce daily bhavcopy ingestion cadence and keep validation/anomaly reporting strict on missing price coverage.
- Owner: Data pipeline + operator
- Status: Open

## R-011: Live Order Accidental Submission
- Severity: Critical
- Description: terminal users can unintentionally place live orders if mode/confirmation controls are weak.
- Mitigation: require explicit `is_live_confirmation_ack` on LIVE order API path and block when missing.
- Owner: Terminal + trading backend
- Status: Mitigated

## R-012: Risk Warning Overrides Without Audit
- Severity: High
- Description: allowing risk warnings without recording rationale weakens post-trade auditability.
- Mitigation: require `risk_override_reason` whenever risk status is WARNING and override is used.
- Owner: Trading backend
- Status: Mitigated

## R-013: Options Board Data Freshness Drift
- Severity: High
- Description: option chain/depth/orderflow can appear fresh while stale due API lag/network failures.
- Mitigation: include freshness timestamps, stale banners, and mixed polling+websocket model in terminal.
- Owner: Terminal + options services
- Status: Open
