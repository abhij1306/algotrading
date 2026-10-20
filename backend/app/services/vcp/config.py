"""VCP strategy settings: defaults, sizing parameters and the `system_config` keys.

Scan and regime take a `VCPParams` instead of reading module globals, so tests can pass
their own. Runtime settings live as key/value rows in `system_config`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from ...models import SystemConfig

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE = "NIFTY500"
DEFAULT_CAPITAL = 1_000_000.0
RISK_PCT_BULL = 0.01
RISK_PCT_BEAR = 0.005
MAX_POSITION_CAP = 0.20
GAP_UP_MAX_PCT = 0.02
TRAIL_SLIPPAGE_PCT = 0.001

# `system_config` keys.
CAPITAL_KEY = "DEFAULT_CAPITAL"
LAST_SCAN_DATE_KEY = "LAST_VCP_SCAN_DATE"
LAST_SCAN_UNIVERSE_KEY = "LAST_VCP_SCAN_UNIVERSE"
LAST_SCAN_RESULT_COUNT_KEY = "LAST_VCP_SCAN_RESULT_COUNT"
# Per universe: "LAST_VCP_SCAN_RUN:NIFTY500" holds the latest completed scan as JSON.
LAST_SCAN_RUN_KEY_PREFIX = "LAST_VCP_SCAN_RUN:"
TRADING_HALTED_KEY = "TRADING_HALTED"
HALT_REASON_KEY = "HALT_REASON"
HALTED_AT_KEY = "HALTED_AT"


@dataclass(frozen=True)
class VCPParams:
    """Position sizing and regime risk settings."""

    default_capital: float = DEFAULT_CAPITAL
    risk_pct_bull: float = RISK_PCT_BULL
    risk_pct_bear: float = RISK_PCT_BEAR
    max_position_cap: float = MAX_POSITION_CAP


DEFAULT_PARAMS = VCPParams()


def get_config(db: Session, key: str, default: str) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return row.value if row else default


def set_config(db: Session, key: str, value: str) -> None:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row is None:
        row = SystemConfig(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def configured_capital(db: Session, params: VCPParams = DEFAULT_PARAMS) -> float:
    """The scan capital from `system_config`, or `params.default_capital` when unset."""
    return float(get_config(db, CAPITAL_KEY, str(params.default_capital)))


def last_scan_date(db: Session) -> date | None:
    """The date the last scan recorded in `system_config`, or None when none ran."""
    value = get_config(db, LAST_SCAN_DATE_KEY, "")
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@dataclass(frozen=True)
class ScanRun:
    """One completed scan of a universe, stored whether or not it found signals."""

    scan_id: str
    scan_date: date
    scan_timestamp: datetime | None


def _scan_run_key(universe: str) -> str:
    return f"{LAST_SCAN_RUN_KEY_PREFIX}{universe}"


def record_scan_run(db: Session, universe: str, run: ScanRun) -> None:
    """Store `run` as the latest completed scan of `universe` (a normalised code)."""
    value = {
        "scan_id": run.scan_id,
        "scan_date": run.scan_date.isoformat(),
        "scan_timestamp": run.scan_timestamp.isoformat() if run.scan_timestamp else None,
    }
    set_config(db, _scan_run_key(universe), json.dumps(value))


def last_scan_run(db: Session, universe: str) -> ScanRun | None:
    """The latest completed scan of `universe`, or None when none was recorded."""
    value = get_config(db, _scan_run_key(universe), "")
    if not value:
        return None
    try:
        stored = json.loads(value)
        timestamp = stored["scan_timestamp"]
        return ScanRun(
            scan_id=str(stored["scan_id"]),
            scan_date=date.fromisoformat(stored["scan_date"]),
            scan_timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("Ignoring an unreadable VCP scan-run marker", extra={"universe": universe})
        return None
