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
