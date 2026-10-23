"""Hire a Trader (HAT): the backend's one bounded module.

The map is `docs/architecture.md`; the rules are `docs/invariants.md`. Package layout:
`data`, `engine`, `risk`, `execution`, `telegram`, `ops` and `models`. Nothing outside this
package imports it except `app.database` (schema upkeep) and `app.main` (the lifespan wiring and the
routes). HAT imports nothing but the shared modules listed in CONF `imports.*`, never `app.main`.
"""
