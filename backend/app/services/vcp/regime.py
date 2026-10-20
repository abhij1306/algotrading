"""Market regime: NIFTY50 against its 200-day moving average, as of a date.

BULL when the close is at or above the average, BEAR below it; the regime sets the
risk per trade. Only bars on or before the requested date are read, so a past date never
sees later prices. Index history comes through the `IndexHistory` port.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import pandas as pd

from ...exceptions import DataUnavailableError
from .config import DEFAULT_PARAMS, VCPParams

logger = logging.getLogger(__name__)

REGIME_INDEX = "NIFTY50"
REGIME_DATA_UNAVAILABLE = "Market regime unavailable: NIFTY50 index history is missing"
MA_WINDOW = 200
LOOKBACK_BARS = 260


class IndexHistory(Protocol):
    def load_index_history(
        self,
        universe_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Daily index bars with at least `date` and `close` columns."""
        ...


@dataclass
class RegimeSnapshot:
    scan_date: date
    regime: str
    close: float
    ma_200: float
    risk_pct: float


def regime_from_history(
    history: pd.DataFrame, as_of: date | None = None, params: VCPParams = DEFAULT_PARAMS
) -> RegimeSnapshot:
    """The regime on `as_of` (default: the last bar) from daily index bars.

    With fewer than 200 bars the average falls back to the close, which reads as BULL.
    Raises DataUnavailableError when there is no bar on or before `as_of`.
    """
    if history.empty:
        logger.warning("Regime unavailable: NIFTY50 index history is missing")
        raise DataUnavailableError(REGIME_DATA_UNAVAILABLE)
    scan_date = as_of or history["date"].max()
    scoped = history[history["date"] <= scan_date].sort_values("date").tail(LOOKBACK_BARS).copy()
    if scoped.empty:
        logger.warning("Regime unavailable: no NIFTY50 index history on or before %s", scan_date)
        raise DataUnavailableError(REGIME_DATA_UNAVAILABLE)
    scoped["ma_200"] = scoped["close"].rolling(MA_WINDOW).mean()
    latest = scoped.iloc[-1]
    close = float(latest["close"])
    ma_200 = close if pd.isna(latest["ma_200"]) else float(latest["ma_200"])
    bull = close >= ma_200
    return RegimeSnapshot(
        scan_date=scan_date,
        regime="BULL" if bull else "BEAR",
        close=round(close, 2),
        ma_200=round(ma_200, 2),
        risk_pct=params.risk_pct_bull if bull else params.risk_pct_bear,
    )


def serialize_regime(regime: RegimeSnapshot) -> dict[str, Any]:
    """The API shape: ISO date, rounded levels and `risk_pct` as a percentage."""
    return {
        "scan_date": regime.scan_date.isoformat(),
        "regime": regime.regime,
        "close": regime.close,
        "ma_200": regime.ma_200,
        "risk_pct": round(regime.risk_pct * 100.0, 2),
    }


class RegimeService:
    def __init__(self, history: IndexHistory, params: VCPParams = DEFAULT_PARAMS) -> None:
        self._history = history
        self._params = params

    def get_regime(self, as_of: date | None = None) -> RegimeSnapshot:
        return regime_from_history(
            self._history.load_index_history(REGIME_INDEX), as_of, self._params
        )

    def summary(self, as_of: date | None = None) -> dict[str, Any]:
        return serialize_regime(self.get_regime(as_of))
