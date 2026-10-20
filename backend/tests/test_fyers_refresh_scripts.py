"""Characterization tests for the FYERS refresh scripts' main() (broker and clock faked)."""

import importlib.util
import json
from argparse import Namespace
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.time = SimpleNamespace(sleep=lambda _s: None)
    module.symbol_master = SimpleNamespace(to_fyers=lambda s: f"NSE:{s}-X")
    return module


def _epoch(day: date, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp())


def _candle(day: date, close: float, hour: int = 0, minute: int = 0) -> list:
    return [_epoch(day, hour, minute), close - 1, close + 1, close - 2, close, 100]


def _printed_json(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    payload.pop("generated_at", None)
    return payload


class FakeHistory:
    def __init__(self, replies: dict[str, object]) -> None:
        self.replies = replies
        self.calls: list[tuple] = []

    def __call__(self, fyers_symbol: str, **kwargs: object) -> object:
        self.calls.append((fyers_symbol, *kwargs.values()))
        reply = self.replies[fyers_symbol]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# ---------------------------------------------------------------- equity daily


@pytest.fixture
def equity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load("refresh_fyers_equity_daily")
    out = tmp_path / "data_system" / "01_sources" / "fyers_stock_prices"
    out.mkdir(parents=True)
    module.PROJECT_ROOT = tmp_path
    module.OUT_DIR = out
    module.OUT_YEAR_DIR = out / "yearly"
    module.OUT_PARQUET = out / "stock_price_daily.parquet"
    module.OUT_CSV = out / "stock_price_daily.csv"
    module.MANIFEST_PATH = out / "manifest.json"
    module.FAILED_PATH = out / "failed.json"
    module.database_max_date = lambda: date(2026, 1, 2)
    return module


def _equity_args(**overrides: object) -> Namespace:
    values = {
        "universes": "",
        "symbols": "ddd, aaa,bbb,ccc,aaa",
        "start_date": "",
        "end_date": "2026-01-08",
        "throttle": 0.0,
        "limit_symbols": 0,
        "dry_run": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_equity_refresh_merges_and_reports(
    equity: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = pd.DataFrame(
        [
            {"date": date(2026, 1, 5), "symbol": "AAA", "close": 1.0, "source": "fyers"},
            {"date": date(2026, 1, 6), "symbol": "AAA", "close": 2.0, "source": "fyers"},
        ]
    )
    monkeypatch.setattr(equity, "load_existing_dataset", lambda: existing.copy())
    monkeypatch.setattr(equity, "parse_args", lambda: _equity_args())
    history = FakeHistory(
        {
            "NSE:AAA-X": {"s": "ok", "candles": [_candle(date(2026, 1, 6), 20.0)]},
            "NSE:BBB-X": {"s": "error", "message": "no data"},
            "NSE:CCC-X": RuntimeError("token expired"),
            "NSE:DDD-X": {"s": "ok", "candles": [["bad"]]},
        }
    )
    monkeypatch.setattr(equity, "fetch_history", history)

    equity.main()

    summary = _printed_json(capsys)
    assert [call[:3] for call in history.calls] == [
        ("NSE:AAA-X", date(2026, 1, 7), date(2026, 1, 8)),
        ("NSE:BBB-X", date(2026, 1, 7), date(2026, 1, 8)),
        ("NSE:CCC-X", date(2026, 1, 7), date(2026, 1, 8)),
        ("NSE:DDD-X", date(2026, 1, 7), date(2026, 1, 8)),
    ]
    assert {k: summary[k] for k in summary if k != "field_definitions"} == {
        "source_contract": {"mode": "explicit_symbols", "universes": []},
        "requested_symbol_count": 4,
        "db_max_date_before": "2026-01-02",
        "dataset_max_date_before": "2026-01-06",
        "range_from": "2026-01-07",
        "requested_range_to": "2026-01-08",
        "output_parquet": str(
            Path("data_system/01_sources/fyers_stock_prices/stock_price_daily.parquet")
        ),
        "output_csv": str(Path("data_system/01_sources/fyers_stock_prices/stock_price_daily.csv")),
        "rows_total": 2,
        "dataset_unique_symbols_total": 1,
        "min_date": "2026-01-05",
        "max_date": "2026-01-06",
        "failed_symbols": 3,
    }
    failures = json.loads(equity.FAILED_PATH.read_text(encoding="utf-8"))
    assert failures == {
        "BBB": {"s": "error", "message": "no data"},
        "CCC": {"error": "token expired"},
        # Candles returned but all malformed counts as failed (PR #34 review).
        "DDD": {"error": "no valid candles", "candles": 1},
    }
    written = pd.read_parquet(equity.OUT_PARQUET)
    assert written[["symbol", "close", "year"]].values.tolist() == [
        ["AAA", 1.0, 2026],
        ["AAA", 20.0, 2026],
    ]
    assert (equity.OUT_YEAR_DIR / "stock_price_daily_2026.parquet").exists()
    assert json.loads(equity.MANIFEST_PATH.read_text(encoding="utf-8"))["rows_total"] == 2


@pytest.mark.parametrize(
    ("args", "status", "range_from"),
    [
        ({"dry_run": True, "limit_symbols": 2}, "dry_run", "2026-01-03"),
        ({"start_date": "2026-02-01"}, "noop", "2026-02-01"),
    ],
)
def test_equity_refresh_dry_run_and_noop(
    equity: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: dict,
    status: str,
    range_from: str,
) -> None:
    monkeypatch.setattr(equity, "parse_args", lambda: _equity_args(**args))
    monkeypatch.setattr(
        equity, "fetch_history", lambda **_k: pytest.fail("must not fetch in dry run / noop")
    )

    equity.main()

    summary = _printed_json(capsys)
    assert summary["status"] == status
    assert summary["range_from"] == range_from
    assert summary["dataset_max_date_before"] is None
    assert summary["requested_symbol_count"] == (2 if args.get("limit_symbols") else 4)
    assert not equity.MANIFEST_PATH.exists()


# ----------------------------------------------------------------- index daily


@pytest.fixture
def index_script(tmp_path: Path) -> ModuleType:
    module = _load("refresh_fyers_index_daily")
    out = tmp_path / "data_system" / "01_sources" / "fyers_index_prices"
    module.PROJECT_ROOT = tmp_path
    module.OUT_DIR = out
    module.OUT_PARQUET = out / "index.parquet"
    module.OUT_CSV = out / "index.csv"
    module.SUMMARY_PATH = out / "summary.json"
    module.MANIFEST_PATH = out / "manifest.json"
    return module


def _index_args(**overrides: object) -> Namespace:
    values = {
        "universes": "NIFTY50,BANKNIFTY,FINNIFTY,MIDCAP,DONE",
        "start_date": "",
        "end_date": "2026-01-08",
        "throttle": 0.0,
        "dry_run": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_index_refresh(
    index_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = pd.DataFrame(
        [
            {"date": date(2026, 1, 5), "universe_id": "NIFTY50", "close": 1.0},
            {"date": date(2026, 1, 8), "universe_id": "DONE", "close": 3.0},
        ]
    )
    monkeypatch.setattr(index_script, "load_existing_dataset", lambda: existing.copy())
    monkeypatch.setattr(index_script, "parse_args", lambda: _index_args())
    history = FakeHistory(
        {
            "NSE:NIFTY50-X": {
                "s": "ok",
                "candles": [_candle(date(2026, 1, 6), 10.0), _candle(date(2026, 1, 7), 11.0)],
            },
            "NSE:BANKNIFTY-X": {"s": "ok", "candles": [["x", 1, 2, 3, 4, 5]]},
            "NSE:FINNIFTY-X": {"s": "error"},
            "NSE:MIDCAP-X": RuntimeError("boom"),
        }
    )
    monkeypatch.setattr(index_script, "fetch_history", history)

    assert index_script.main() == 0

    manifest = _printed_json(capsys)
    assert [call[:3] for call in history.calls] == [
        ("NSE:NIFTY50-X", date(2026, 1, 6), date(2026, 1, 8)),
        ("NSE:BANKNIFTY-X", date(2016, 1, 1), date(2026, 1, 8)),
        ("NSE:FINNIFTY-X", date(2016, 1, 1), date(2026, 1, 8)),
        ("NSE:MIDCAP-X", date(2016, 1, 1), date(2026, 1, 8)),
    ]
    assert manifest["refresh_summary"] == {
        "NIFTY50": {
            "fyers_symbol": "NSE:NIFTY50-X",
            "range_from": "2026-01-06",
            "range_to": "2026-01-08",
            "rows_added": 2,
        },
        "BANKNIFTY": {
            "fyers_symbol": "NSE:BANKNIFTY-X",
            "range_from": "2016-01-01",
            "range_to": "2026-01-08",
            "rows_added": 0,
        },
        "FINNIFTY": {
            "fyers_symbol": "NSE:FINNIFTY-X",
            "range_from": "2016-01-01",
            "range_to": "2026-01-08",
        },
        "MIDCAP": {
            "fyers_symbol": "NSE:MIDCAP-X",
            "range_from": "2016-01-01",
            "range_to": "2026-01-08",
        },
        "DONE": {
            "fyers_symbol": "NSE:DONE-X",
            "range_from": "2026-01-09",
            "range_to": "2026-01-08",
        },
    }
    assert manifest["failed_universes"] == [
        {"universe_id": "FINNIFTY", "fyers_symbol": "NSE:FINNIFTY-X", "response": {"s": "error"}},
        {"universe_id": "MIDCAP", "fyers_symbol": "NSE:MIDCAP-X", "error": "boom"},
    ]
    assert (manifest["rows_total"], manifest["universes_total"], manifest["status"]) == (
        4,
        2,
        "completed",
    )
    assert (manifest["min_date"], manifest["max_date"]) == ("2026-01-05", "2026-01-08")
    assert manifest["universes_processed"] == 5
    assert json.loads(index_script.SUMMARY_PATH.read_text(encoding="utf-8"))["rows_total"] == 4
    written = pd.read_parquet(index_script.OUT_PARQUET)
    assert written["universe_id"].tolist() == ["NIFTY50", "NIFTY50", "NIFTY50", "DONE"]


def test_index_refresh_dry_run_with_start_date(
    index_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        index_script,
        "parse_args",
        lambda: _index_args(universes="NIFTY50", start_date="2026-01-02", dry_run=True),
    )
    monkeypatch.setattr(index_script, "fetch_history", lambda **_k: pytest.fail("no fetch"))

    assert index_script.main() == 0

    manifest = _printed_json(capsys)
    assert manifest["status"] == "dry_run"
    assert manifest["rows_total"] == 0
    assert manifest["refresh_summary"]["NIFTY50"]["range_from"] == "2026-01-02"
    assert not index_script.OUT_PARQUET.exists()


# ------------------------------------------------------------ intraday archive


@pytest.fixture
def intraday(tmp_path: Path) -> ModuleType:
    module = _load("refresh_fyers_intraday_archive")
    module.PROJECT_ROOT = tmp_path
    module.ARCHIVE_ROOT = tmp_path / "archive" / "fyers_intraday"
    return module


def _intraday_args(**overrides: object) -> Namespace:
    values = {
        "universe": "NIFTY500",
        "symbols": "bbb,aaa",
        "timeframe": "5",
        "start_year": 2025,
        "years": 2,
        "start_date": "2025-01-01",
        "end_date": "2025-05-01",
        "throttle": 0.0,
        "limit_symbols": 0,
        "dry_run": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_intraday_archive(
    intraday: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(intraday, "parse_args", lambda: _intraday_args())
    ok_a1 = {"s": "ok", "candles": [_candle(date(2025, 1, 2), 5.0, 4, 0)] * 2}
    ok_a2 = {"s": "ok", "candles": [_candle(date(2025, 4, 15), 6.0, 4, 0)]}
    history = FakeHistory(
        {
            "NSE:AAA-X": [ok_a1, ok_a2],
            "NSE:BBB-X": [{"s": "ok", "candles": []}, {"s": "error", "code": 1}],
        }
    )
    monkeypatch.setattr(intraday, "fetch_history", history)

    intraday.main()

    summary = _printed_json(capsys)
    assert [call[:4] for call in history.calls] == [
        ("NSE:AAA-X", "5", date(2025, 1, 1), date(2025, 4, 10)),
        ("NSE:AAA-X", "5", date(2025, 4, 11), date(2025, 5, 1)),
        ("NSE:BBB-X", "5", date(2025, 1, 1), date(2025, 4, 10)),
        ("NSE:BBB-X", "5", date(2025, 4, 11), date(2025, 5, 1)),
    ]
    out_root = Path("archive/fyers_intraday/explicit_symbols/timeframe=5min")
    assert summary == {
        "scope": "explicit_symbols",
        "timeframe": "5",
        "symbol_count": 2,
        "windows": [
            {
                "year": 2025,
                "range_from": "2025-01-01",
                "range_to": "2025-05-01",
                "rows": 2,
                "symbols": 1,
                "failed_symbols": 1,
                "output_parquet": str(out_root / "explicit_symbols_5min_2025.parquet"),
                "failure_log": str(out_root / "explicit_symbols_5min_2025_failures.json"),
            }
        ],
        "output_root": str(out_root),
        "status": "completed",
    }
    root = intraday.PROJECT_ROOT / out_root
    failures = json.loads((root / "explicit_symbols_5min_2025_failures.json").read_text("utf-8"))
    assert failures == [
        {
            "symbol": "BBB",
            "fyers_symbol": "NSE:BBB-X",
            "year": 2025,
            "chunk_from": "2025-04-11",
            "chunk_to": "2025-05-01",
            "response": {"s": "error", "code": 1},
        }
    ]
    frame = pd.read_parquet(root / "explicit_symbols_5min_2025.parquet")
    assert frame["timestamp"].astype(str).tolist() == ["2025-01-02 09:30:00", "2025-04-15 09:30:00"]
    assert json.loads((root / "manifest.json").read_text("utf-8"))["symbol_count"] == 2


def test_intraday_archive_all_failed_writes_empty_year(
    intraday: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        intraday,
        "parse_args",
        lambda: _intraday_args(symbols="aaa", end_date="2025-01-31"),
    )
    monkeypatch.setattr(intraday, "fetch_history", FakeHistory({"NSE:AAA-X": ["not a dict"]}))

    intraday.main()

    window = _printed_json(capsys)["windows"][0]
    assert (window["rows"], window["symbols"], window["failed_symbols"]) == (0, 0, 1)


def test_intraday_archive_dry_run_by_years(
    intraday: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        intraday,
        "parse_args",
        lambda: _intraday_args(symbols="", start_date="", end_date="", dry_run=True),
    )
    monkeypatch.setattr(intraday, "resolve_symbols", lambda universe, explicit: ["X", "Y", "Z"])

    intraday.main()

    summary = _printed_json(capsys)
    assert summary["scope"] == "NIFTY500"
    assert summary["status"] == "dry_run"
    assert summary["symbol_count"] == 3
    assert summary["output_root"] == str(Path("archive/fyers_intraday/nifty500/timeframe=5min"))
    assert summary["windows"] == [
        {"year": 2025, "range_from": "2025-01-01", "range_to": "2025-12-31"},
        {"year": 2024, "range_from": "2024-01-01", "range_to": "2024-12-31"},
    ]
    assert not intraday.ARCHIVE_ROOT.exists()
