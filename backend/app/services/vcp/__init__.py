"""VCP (volatility contraction pattern) strategy package.

`pattern` holds the pure pattern-detection and relative-strength functions.
`positions` holds open positions, manual close/stop, halt and status, reading prices
through the `quotes.QuoteSource` port; `config` holds the `system_config` helpers.
Scan, regime and backtest still live in `app.services.vcp_service`
and move here in later slices (docs/plans/architecture/screener-backtest.md, section 4).
"""
