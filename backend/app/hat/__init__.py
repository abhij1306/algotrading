"""Hire a Trader (HAT): the backend's one bounded module.

The specs in `docs/specs/hat/` are the source of truth. Package layout (00-overview §3):
`data`, `engine`, `risk`, `execution`, `telegram`, `ops` and `models`. Nothing outside this
package imports it except `app.database` (schema upkeep) and `app.main` (the lifespan wiring and the
routes). HAT imports nothing but the shared modules listed in CONF `imports.*`, never `app.main`.
"""
