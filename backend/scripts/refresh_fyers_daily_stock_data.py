"""
Deprecated compatibility wrapper.

Canonical command:
    python backend/scripts/refresh_fyers_equity_daily.py
"""

from __future__ import annotations

from refresh_fyers_equity_daily import main


if __name__ == "__main__":
    raise SystemExit(main())
