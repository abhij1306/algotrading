"""Characterization tests for YFinanceService.get_quotes and the .env loader."""

import os
from pathlib import Path

import pandas as pd
import pytest

from app.services import yfinance_service as yfs_module
from app.services.yfinance_service import YFinanceService
from app.utils.env_loader import load_dotenv


def _bars(rows: list[tuple[float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(
        {
            "Open": [o for o, _ in rows],
            "High": [o + 1 for o, _ in rows],
            "Low": [o - 1 for o, _ in rows],
            "Close": [c for _, c in rows],
            "Volume": [1000.0] * len(rows),
        },
        index=idx,
    )


def test_get_quotes_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = {
        "^NSEI": _bars([(100.0, 100.0), (101.0, 110.0)]),
        "^NSEBANK": _bars([(50.0, float("nan"))]),
        "SBIN.NS": _bars([(0.0, 0.0), (5.0, float("nan"))]),
        "EMPTY.NS": pd.DataFrame(
            {"Open": [float("nan")], "Close": [float("nan")]},
            index=pd.date_range("2026-01-01", periods=1),
        ),
    }
    requested: list[list[str]] = []

    def download(symbols: list[str], **_kwargs: object) -> pd.DataFrame:
        requested.append(symbols)
        return pd.concat({s: frames[s] for s in symbols if s in frames}, axis=1)

    monkeypatch.setattr(yfs_module.yf, "download", download)

    quotes = YFinanceService.get_quotes(["NIFTY50", "BANKNIFTY", "SBIN", "EMPTY", "MISSING"])

    assert requested == [["^NSEI", "^NSEBANK", "SBIN.NS", "EMPTY.NS", "MISSING.NS"]]
    assert quotes["NIFTY50"] == {
        "price": 110.0,
        "change_pct": 10.0,
        "volume": 1000,
        "open": 101.0,
        "high": 102.0,
        "low": 100.0,
        "source": "yfinance",
    }
    assert (quotes["BANKNIFTY"]["price"], quotes["BANKNIFTY"]["change_pct"]) == (50.0, 0.0)
    assert (quotes["SBIN"]["price"], quotes["SBIN"]["change_pct"]) == (5.0, 0.0)
    assert quotes["EMPTY"] is None
    assert quotes["MISSING"] is None


def test_get_quotes_single_symbol_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        yfs_module.yf, "download", lambda *a, **k: _bars([(10.0, 10.0), (10.0, 12.0)])
    )
    assert YFinanceService.get_quote("TCS")["change_pct"] == pytest.approx(20.0)

    def boom(*_a: object, **_k: object) -> pd.DataFrame:
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(yfs_module.yf, "download", boom)
    assert YFinanceService.get_quotes(["TCS"]) == {}


def test_load_dotenv_parses_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "test.env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "",
                "CHAR_PLAIN = value one ",
                'CHAR_DQ="double quoted"',
                "CHAR_SQ='single quoted'",
                "export CHAR_EXPORTED=yes",
                "CHAR_MIXED=\"mismatch'",
                'CHAR_LONE="',
                "CHAR_EQ=a=b",
                "=no key",
                "no equals sign",
                "CHAR_KEEP=from file",
            ]
        ),
        encoding="utf-8",
    )
    names = [
        "CHAR_PLAIN",
        "CHAR_DQ",
        "CHAR_SQ",
        "CHAR_EXPORTED",
        "CHAR_MIXED",
        "CHAR_LONE",
        "CHAR_EQ",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHAR_KEEP", "from env")

    load_dotenv(str(env))

    assert {name: os.environ[name] for name in names} == {
        "CHAR_PLAIN": "value one",
        "CHAR_DQ": "double quoted",
        "CHAR_SQ": "single quoted",
        "CHAR_EXPORTED": "yes",
        "CHAR_MIXED": "\"mismatch'",
        "CHAR_LONE": "",
        "CHAR_EQ": "a=b",
    }
    assert os.environ["CHAR_KEEP"] == "from env"


def test_load_dotenv_falls_back_to_cwd_and_ignores_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "char_cwd.env").write_text("CHAR_FROM_CWD=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHAR_FROM_CWD", raising=False)

    load_dotenv("char_cwd.env")
    load_dotenv("definitely_missing_char.env")

    assert os.environ["CHAR_FROM_CWD"] == "1"
