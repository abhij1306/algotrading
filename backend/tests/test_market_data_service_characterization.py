"""Characterization tests for MarketDataService (no network: yfinance and requests are faked)."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.services import market_data_service as mds
from app.services.market_data_service import MarketDataService

INDEX_SYMBOLS = ["^GSPC", "^IXIC", "^DJI", "^NSEI", "^NSEBANK", "GC=F", "SI=F", "^INDIAVIX", "^VIX"]


def _bars(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"Open": opens or closes, "Close": closes}, index=idx)


def test_global_indices_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = {
        "^GSPC": _bars([100.0, 110.0]),
        "^IXIC": _bars([100.0, float("nan")], opens=[100.0, 95.0]),
        "^DJI": _bars([50.0, 40.0, float("nan")], opens=[50.0, 40.0, float("nan")]),
        "^NSEI": _bars([200.0]),
        "^NSEBANK": _bars([0.0, 10.0]),
    }
    data = pd.concat(frames, axis=1)
    monkeypatch.setattr(mds.yf, "download", lambda *a, **k: data)

    rows = MarketDataService.get_global_indices()

    assert [(r["name"], r["price"], r["change"], r["status"]) for r in rows] == [
        ("S&P 500", 110.0, 10.0, "POSITIVE"),
        ("Nasdaq", 95.0, -5.0, "NEGATIVE"),
        ("Dow Jones", 40.0, -10.0, "NEGATIVE"),
        ("Nifty 50", 200.0, 0.0, "POSITIVE"),
        ("Bank Nifty", 10.0, 10.0, "POSITIVE"),
    ]
    assert rows[0]["change_pct"] == pytest.approx(10.0)
    assert rows[2]["change_pct"] == pytest.approx(-20.0)
    assert rows[4]["change_pct"] == 0


def test_global_indices_download_failure_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> pd.DataFrame:
        raise RuntimeError("down")

    monkeypatch.setattr(mds.yf, "download", boom)
    assert MarketDataService.get_global_indices() == []


class _Resp:
    def __init__(self, status_code: int, payload: object = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> object:
        return self._payload


def _fake_vix(monkeypatch: pytest.MonkeyPatch, closes: list[float] | Exception) -> None:
    def ticker(_symbol: str) -> SimpleNamespace:
        def history(period: str) -> pd.DataFrame:
            if isinstance(closes, Exception):
                raise closes
            return pd.DataFrame({"Close": closes})

        return SimpleNamespace(history=history)

    monkeypatch.setattr(mds.yf, "Ticker", ticker)


def _fake_http(
    monkeypatch: pytest.MonkeyPatch, cnn: _Resp | Exception, tickertape: _Resp | Exception
) -> None:
    def pick(resp: _Resp | Exception) -> _Resp:
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(mds.requests.Session, "get", lambda self, *a, **k: pick(cnn))
    monkeypatch.setattr(mds.requests, "get", lambda *a, **k: pick(tickertape))


CNN_OK = _Resp(200, {"fear_and_greed": {"score": 61.6, "rating": "greed"}})


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        (["77.10", "77.10", "12.00"], (77, "Extreme Greed")),
        (["62.40"], (62, "Greed")),
        (["50.00"], (50, "Neutral")),
        (["38.20"], (38, "Fear")),
        (["24.90", "24.90", "99.00"], (25, "Extreme Fear")),
    ],
)
def test_sentiment_tickertape(
    monkeypatch: pytest.MonkeyPatch, scores: list[str], expected: tuple[int, str]
) -> None:
    html = "".join(f"<p>{s}</p>" for s in ["09.15", "1234.56", *scores]).encode()
    _fake_http(monkeypatch, CNN_OK, _Resp(200, content=html))
    _fake_vix(monkeypatch, RuntimeError("must not be called"))

    result = MarketDataService.get_market_sentiment()

    assert result["us_fear_greed"] == {"score": 62, "status": "Greed", "source": "cnn"}
    assert result["india_sentiment"] == {
        "score": expected[0],
        "status": expected[1],
        "source": "tickertape",
    }


@pytest.mark.parametrize(
    ("vix", "expected"),
    [
        (11.0, (75, "Extreme Greed")),
        (15.0, (50, "Neutral")),
        (20.0, (30, "Fear")),
        (30.0, (15, "Extreme Fear")),
    ],
)
def test_sentiment_vix_fallback_when_no_mmi_score(
    monkeypatch: pytest.MonkeyPatch, vix: float, expected: tuple[int, str]
) -> None:
    _fake_http(monkeypatch, _Resp(500), _Resp(200, content=b"<p>no score</p>"))
    _fake_vix(monkeypatch, [10.0, vix + 0.004])

    result = MarketDataService.get_market_sentiment()

    assert result["us_fear_greed"]["source"] == "unavailable"
    assert result["india_sentiment"] == {
        "score": expected[0],
        "status": expected[1],
        "vix": round(vix + 0.004, 2),
        "source": "vix_fallback",
    }


def test_sentiment_vix_fallback_when_request_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_http(monkeypatch, RuntimeError("cnn"), RuntimeError("tickertape"))
    _fake_vix(monkeypatch, [16.0])

    result = MarketDataService.get_market_sentiment()

    assert result["india_sentiment"]["source"] == "vix_fallback"
    assert result["india_sentiment"]["score"] == 50


def test_sentiment_non_200_tickertape_skips_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_http(monkeypatch, _Resp(500), _Resp(503))
    _fake_vix(monkeypatch, [11.0])

    result = MarketDataService.get_market_sentiment()

    assert result["india_sentiment"] == {
        "score": None,
        "status": "Unavailable",
        "source": "unavailable",
    }


@pytest.mark.parametrize("vix", [[], RuntimeError("vix down")])
def test_sentiment_unavailable_when_vix_fails(
    monkeypatch: pytest.MonkeyPatch, vix: list[float] | Exception
) -> None:
    _fake_http(monkeypatch, _Resp(500), RuntimeError("tickertape"))
    _fake_vix(monkeypatch, vix)

    assert MarketDataService.get_market_sentiment()["india_sentiment"]["source"] == "unavailable"


def _nifty(closes: np.ndarray, multi: bool = False) -> pd.DataFrame:
    idx = pd.date_range("2025-06-01", periods=len(closes), freq="D")
    df = pd.DataFrame({"High": closes + 1.0, "Low": closes - 1.0, "Close": closes}, index=idx)
    if multi:
        df.columns = pd.MultiIndex.from_product([df.columns, ["^NSEI"]])
    return df


@pytest.mark.parametrize(
    ("closes", "multi", "status", "strength"),
    [
        (np.linspace(100.0, 200.0, 120), False, "Bullish Trend", "Strong"),
        (np.linspace(200.0, 100.0, 120), True, "Bearish Trend", "Strong"),
        (100.0 + np.tile([0.0, 0.5], 60), False, "Consolidation", "Weak"),
        (100.0 + 8.0 * np.sin(np.arange(120) / 3.0), False, "Sideways / Range-bound", "Weak"),
    ],
)
def test_market_condition(
    monkeypatch: pytest.MonkeyPatch, closes: np.ndarray, multi: bool, status: str, strength: str
) -> None:
    monkeypatch.setattr(mds.yf, "download", lambda *a, **k: _nifty(closes, multi))

    result = MarketDataService.get_market_condition()

    assert (result["status"], result["trend_strength"]) == (status, strength)
    assert result["technical_summary"].startswith(f"ADX is {result['adx']:.1f}")


def test_market_condition_without_data_or_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mds.yf, "download", lambda *a, **k: pd.DataFrame())
    assert MarketDataService.get_market_condition() == {
        "status": "Unknown",
        "adx": 0,
        "reason": "No data",
    }

    monkeypatch.setattr(mds.yf, "download", lambda *a, **k: pd.DataFrame({"Close": [1.0]}))
    assert MarketDataService.get_market_condition() == {"status": "Unavailable", "adx": 0}
