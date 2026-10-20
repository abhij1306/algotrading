"""VCP (volatility contraction pattern) strategy package.

- `config`: defaults, `VCPParams` and the `system_config` keys and helpers.
- `pattern`: pure pattern-detection and relative-strength functions.
- `universe`: point-in-time index membership and company metadata.
- `regime`: NIFTY50 vs its 200-day average, read through the `IndexHistory` port.
- `scan`: the universe scan, signal sizing, ranking and stored-signal views.
- `positions`: open positions, manual close/stop, halt and status, priced through the
  `quotes.QuoteSource` port.

`app.services.vcp_service.VCPService` wires these to the production adapters for the
router. The backtest still lives there and moves here in a later slice
(docs/plans/architecture/screener-backtest.md, section 4).
"""
