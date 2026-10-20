"""VCP strategy settings: defaults, sizing parameters and the `system_config` keys.

Scan and regime take a `VCPParams` instead of reading module globals, so tests can pass
their own. Runtime settings live as key/value rows in `system_config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from ...models import SystemConfig

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
