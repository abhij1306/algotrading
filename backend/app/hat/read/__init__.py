"""HAT's read model: the bots, kill switches, open positions and watchlists as the dashboard
(`api/dashboard.py`) and the Telegram replies (`telegram/reads.py`) both show them.

Each function takes a session and returns frozen dataclasses, reading in batches (no query per
row). Nothing here writes or imports a module that does (CONF `hat_routes_read_only`), so the API
may use it."""
