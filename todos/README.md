# SmartTrader Todo Tracker

## Naming
`{issue_id}-{status}-{priority}-{description}.md`

Example:
`001-ready-p1-phase1-reset-cleanup.md`

## Status
- `pending`: needs triage/approval or blocked context
- `ready`: approved and executable
- `complete`: done and verified

## Priority
- `p1`: critical path
- `p2`: important
- `p3`: nice to have

## Dependency Rule
Use issue IDs in frontmatter `dependencies` array. A todo should not start until all dependencies are `complete`.
