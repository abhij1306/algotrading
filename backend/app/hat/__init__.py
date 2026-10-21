"""Hire a Trader (HAT): a bounded module of the SmartTrader backend.

The specs in `docs/specs/hat/` are the source of truth. Package layout (00-overview §3):
`data`, `engine`, `risk`, `execution`, `telegram`, `ops` and `models`. Nothing outside this
package imports it except `app.database` (schema upkeep) and, from PR 17, the lifespan wiring.
"""
