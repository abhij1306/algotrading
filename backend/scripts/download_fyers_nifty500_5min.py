from __future__ import annotations

"""
Deprecated compatibility wrapper.

Canonical command:
    python backend/scripts/refresh_fyers_intraday_archive.py --universe NIFTY500 --timeframe 5
"""

import sys

from refresh_fyers_intraday_archive import main


if __name__ == "__main__":
    if not any(arg == "--universe" or arg.startswith("--universe=") for arg in sys.argv):
        sys.argv.extend(["--universe", "NIFTY500"])
    if not any(arg == "--timeframe" or arg.startswith("--timeframe=") for arg in sys.argv):
        sys.argv.extend(["--timeframe", "5"])
    raise SystemExit(main())
