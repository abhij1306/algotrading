"""Market regime through RegimeService with a fake index history (no DB, no files)."""

from datetime import date

import pandas as pd
import pytest

from app.exceptions import DataUnavailableError
from app.services.vcp.config import VCPParams
from app.services.vcp.regime import REGIME_DATA_UNAVAILABLE, RegimeService

DATES = [d.date() for d in pd.bdate_range("2024-01-01", periods=260)]


class FakeIndexHistory:
    """Serves one fixed frame for NIFTY50 and nothing for any other index."""

    def __init__(self, closes: list[float]) -> None:
        self.frame = pd.DataFrame({"date": DATES[: len(closes)], "close": closes})

    def load_index_history(
        self, universe_id: str, start_date: date | None = None, end_date: date | None = None
    ) -> pd.DataFrame:
        return self.frame.copy() if universe_id == "NIFTY50" else pd.DataFrame()


RISING = [float(i) for i in range(1, 251)]  # 1..250
FALLING = RISING[::-1]  # 250..1
# Rises to 220 on bar 220, then collapses to 10: a later crash.
PEAK_THEN_CRASH = RISING[:220] + [10.0] * 30


@pytest.mark.parametrize(
    ("closes", "as_of", "params", "expected"),
    [
        pytest.param(
            RISING,
            None,
            VCPParams(),
            {
                "scan_date": "2024-12-13",
                "regime": "BULL",
                "close": 250.0,
                "ma_200": 150.5,
                "risk_pct": 1.0,
            },
            id="close-above-200dma-is-bull",
        ),
        pytest.param(
            FALLING,
            None,
            VCPParams(),
            {
                "scan_date": "2024-12-13",
                "regime": "BEAR",
                "close": 1.0,
                "ma_200": 100.5,
                "risk_pct": 0.5,
            },
            id="close-below-200dma-is-bear",
        ),
        pytest.param(
            PEAK_THEN_CRASH,
            DATES[219],
            VCPParams(),
            {
                "scan_date": "2024-11-01",
                "regime": "BULL",
                "close": 220.0,
                "ma_200": 120.5,
                "risk_pct": 1.0,
            },
            id="past-date-ignores-the-later-crash",
        ),
        pytest.param(
            PEAK_THEN_CRASH,
            None,
            VCPParams(),
            {
                "scan_date": "2024-12-13",
                "regime": "BEAR",
                "close": 10.0,
                "ma_200": 116.67,
                "risk_pct": 0.5,
            },
            id="latest-date-sees-the-crash",
        ),
        pytest.param(
            RISING[:50],
            None,
            VCPParams(),
            {
                "scan_date": "2024-03-08",
                "regime": "BULL",
                "close": 50.0,
                "ma_200": 50.0,
                "risk_pct": 1.0,
            },
            id="short-history-falls-back-to-close",
        ),
        pytest.param(
            FALLING,
            None,
            VCPParams(risk_pct_bull=0.02, risk_pct_bear=0.0025),
            {
                "scan_date": "2024-12-13",
                "regime": "BEAR",
                "close": 1.0,
                "ma_200": 100.5,
                "risk_pct": 0.25,
            },
            id="risk-comes-from-params",
        ),
    ],
)
def test_regime_summary(
    closes: list[float], as_of: date | None, params: VCPParams, expected: dict[str, object]
) -> None:
    service = RegimeService(FakeIndexHistory(closes), params)

    assert service.summary(as_of) == expected


@pytest.mark.parametrize(
    ("closes", "as_of"),
    [
        pytest.param([], None, id="no-index-history"),
        pytest.param(RISING, date(2023, 12, 29), id="no-bar-on-or-before-date"),
    ],
)
def test_regime_without_history_is_data_unavailable(
    closes: list[float], as_of: date | None
) -> None:
    service = RegimeService(FakeIndexHistory(closes))

    with pytest.raises(DataUnavailableError) as excinfo:
        service.get_regime(as_of)

    assert excinfo.value.message == REGIME_DATA_UNAVAILABLE
    assert excinfo.value.status_code == 503
