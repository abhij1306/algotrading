"""
One owner for repository and data paths.

Every module that reads or writes files under the repository imports its root from here
instead of counting `Path(__file__).parents[N]`, so moving a module or the `data_system/`
tree is a one-line change. The Fyers token file is the exception: HAT's token store
(`app.hat.execution.fyers_tokens`) owns its location.
"""

import os
from pathlib import Path

# backend/app/paths.py -> backend/app -> backend -> repository root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_FILE = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"

# The data tree (gitignored); HAT keeps its data under HAT_DATA_ROOT below.
DATA_ROOT = PROJECT_ROOT / "data_system"

# Hire a Trader (HAT): Parquet bars, raw bhavcopy zips and backtest outputs. Gitignored.
# HAT_DATA_ROOT overrides it (HAT-OVR-052); an empty value means the default.
HAT_DATA_ROOT = Path(os.environ.get("HAT_DATA_ROOT", "").strip() or DATA_ROOT / "hat")

# The Backtest Result Standard's JSON schema, which every run's run.json is validated against
# before its folder is published (docs/backtests/result-standard.md; the image copies it).
BACKTEST_SCHEMA = PROJECT_ROOT / "docs" / "backtests" / "run.schema.json"

# D5's reviewed exceptions: results findings that are explained and known (code, isin, period
# end, reason), read by `app/hat/data/verify/filings.py`.
D5_EXCEPTIONS = PROJECT_ROOT / "backend" / "app" / "hat" / "data" / "verify" / "d5_exceptions.csv"
# D2's and D6's reviewed exceptions (code, isin, day, reason), read through
# `app/hat/data/verify/exceptions.py`.
D2_EXCEPTIONS = PROJECT_ROOT / "backend" / "app" / "hat" / "data" / "verify" / "d2_exceptions.csv"
D6_EXCEPTIONS = PROJECT_ROOT / "backend" / "app" / "hat" / "data" / "verify" / "d6_exceptions.csv"
# D3, D4 and D7's reviewed exceptions (code, ISIN or key, day, pin, reason), read through
# `app/hat/data/verify/exceptions.py`.
VERIFY_EXCEPTIONS = {
    check: PROJECT_ROOT / "backend" / "app" / "hat" / "data" / "verify" / f"{check}_exceptions.csv"
    for check in ("d3", "d4", "d7")
}
# The known coverage gaps of the 5-minute history (vendor, span, ISIN, reason), read by
# `app/hat/data/bars5m_gaps.py`: a replay decides a session inside one on the daily estimate.
BARS5M_GAPS = PROJECT_ROOT / "backend" / "app" / "hat" / "data" / "bars5m_gaps.csv"

# The golden sample format, which every golden file is validated against before a check reads it
# (docs/backtests/golden-sample.md; the image copies it).
GOLDEN_SCHEMA = PROJECT_ROOT / "docs" / "backtests" / "golden-sample.schema.json"
