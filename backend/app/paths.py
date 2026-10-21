"""
One owner for repository and data paths.

Every module that reads or writes files under the repository imports its root from here
instead of counting `Path(__file__).parents[N]`, so moving a module or the `data_system/`
tree is a one-line change.
"""

import os
from pathlib import Path

# backend/app/paths.py -> backend/app -> backend -> repository root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_FILE = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
FYERS_CONFIG_DIR = PROJECT_ROOT / "fyers" / "config"
FYERS_TOKEN_PATH = FYERS_CONFIG_DIR / "access_token.json"
INDEX_WEIGHTAGE_DIR = PROJECT_ROOT / "data" / "index_weightage"

# Canonical data tree.
DATA_ROOT = PROJECT_ROOT / "data_system"
SOURCES_ROOT = DATA_ROOT / "01_sources"
UNIVERSE_CONSTITUENTS_DIR = DATA_ROOT / "03_universe" / "constituents"
CURATED_ROOT = DATA_ROOT / "04_curated" / "phase1"
METADATA_ROOT = DATA_ROOT / "05_metadata" / "phase1"

# Hire a Trader (HAT): Parquet bars, raw bhavcopy zips and backtest outputs. Gitignored.
# HAT_DATA_ROOT overrides it (HAT-OVR-052); an empty value means the default.
HAT_DATA_ROOT = Path(os.environ.get("HAT_DATA_ROOT", "").strip() or DATA_ROOT / "hat")
