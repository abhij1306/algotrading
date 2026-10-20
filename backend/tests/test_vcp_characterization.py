"""Characterization tests pinning VCPService pattern analysis, backtest trade
simulation and the backtest loop, captured before a complexity refactor.

tests/fixtures/vcp_backtest_golden.json is run_backtest()'s result for the
fixture below, captured before the refactor.
"""

import json
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.base import Base
from app.models import IndexConstituentHistory, IndexUniverseDefinition
from app.services import vcp_service as vcp_module
from app.services.vcp_service import RegimeSnapshot, VCPService

GOLDEN = Path(__file__).parent / "fixtures" / "vcp_backtest_golden.json"
BULL = RegimeSnapshot(date(2025, 1, 1), "BULL", 100.0, 90.0, 0.01)
BEAR = RegimeSnapshot(date(2025, 1, 1), "BEAR", 100.0, 110.0, 0.005)
META = {"name": "VCP X", "sector": "Tech"}


# --- _analyze_pattern ------------------------------------------------------------


def _segments(anchors: list[tuple[int, float]]) -> list[float]:
    out: list[float] = []
    for (i0, p0), (i1, p1) in zip(anchors, anchors[1:], strict=False):
        out.extend(np.linspace(p0, p1, i1 - i0 + 1)[:-1])
    out.append(anchors[-1][1])
    return out


def vcp_frame() -> pd.DataFrame:
    """300 sessions: a stage-2 uptrend, then a 60-day base with three tightening
    contractions (about 18%, 9% and 4% deep) on drying-up volume."""
    trend = list(np.linspace(40.0, 85.0, 230)) + list(np.linspace(86.5, 98.5, 10))
    base = _segments(
        [(0, 100.0), (6, 82.0), (12, 96.0), (17, 88.3), (22, 93.0), (26, 90.2), (59, 91.0)]
    )
    closes = trend + base
    volume = (
        [1_000_000.0] * 240
        + list(np.linspace(900_000, 500_000, 22))
        + [80_000.0] * 5
        + [120_000.0] * 33
    )
    volume[-1] = 200_000.0
    n = len(closes)
    return pd.DataFrame(
        {
            "symbol": ["VCPX"] * n,
            "date": pd.bdate_range("2024-01-01", periods=n).date,
            "close": closes,
            "close_ref": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "volume": volume,
        }
    )


def with_breakout(frame: pd.DataFrame, close: float, volume: float) -> pd.DataFrame:
    frame = frame.copy()
    frame.loc[frame.index[-1], ["close", "close_ref", "high", "low", "volume"]] = [
        close,
        close,
        close * 1.002,
        close * 0.98,
        volume,
    ]
    return frame


STAGE2 = {
    "ma_50": 90.97399999999999,
    "ma_150": 83.66492867540029,
    "ma_200": 78.86496288209607,
    "high_52": 100.0,
}
BASE_SIGNAL = {
    "symbol": "VCPX",
    "company_name": "VCP X",
    "sector": "Tech",
    "grade": "A",
    "rs_rating": 92,
    "stage2_conditions_met": 5,
    "contraction_count": 3,
    "contraction_depths": [18.41, 8.94, 3.98],
    "final_contraction_depth": 3.98,
    "volume_dry_up_pct": 34.2,
    "pivot_high": 93.46,
    "stop_level": 89.66,
    "stop_pct": 4.07,
    "days_in_base": 60,
    "is_breakout": False,
    "breakout_price": None,
    "breakout_volume_mult": 0.85,
    "close_position_in_range": 0.5,
    "overhead_clear": True,
    "regime": "BULL",
    "signal_status": "SIGNAL",
    "planned_entry": 93.46,
    "planned_shares": 2139,
    "planned_position_value": 199921.63,
    "capital_risk": 8138.89,
    "two_r_target": 101.07,
    "metadata_json": {
        "stage2": STAGE2,
        "volume_slope": -14217.6531,
        "base_depth_pct": 18.0,
        "final_contraction_low": 89.75,
        "stop_offset_pct": 0.001,
        "risk_pct": 0.01,
    },
}


def test_vcp_base_produces_grade_a_signal() -> None:
    result = VCPService()._analyze_pattern(vcp_frame(), 92, META, BULL, 1_000_000.0)

    assert result == BASE_SIGNAL


def test_lower_rs_in_bear_regime_is_grade_b_with_smaller_size() -> None:
    result = VCPService()._analyze_pattern(vcp_frame(), 85, META, BEAR, 500_000.0)

    assert result == {
        **BASE_SIGNAL,
        "grade": "B",
        "rs_rating": 85,
        "regime": "BEAR",
        "planned_shares": 657,
        "planned_position_value": 61406.5,
        "capital_risk": 2499.88,
        "metadata_json": {**BASE_SIGNAL["metadata_json"], "risk_pct": 0.005},
    }


@pytest.mark.parametrize(
    ("volume", "grade", "volume_mult", "dry_up", "slope"),
    [
        (600_000.0, "A", 2.48, 33.07, -13561.9153),
        (400_000.0, "B", 1.68, 33.62, -13889.7842),
    ],
)
def test_breakout_day_grade_depends_on_breakout_volume(
    volume: float, grade: str, volume_mult: float, dry_up: float, slope: float
) -> None:
    frame = with_breakout(vcp_frame(), 94.5, volume)

    result = VCPService()._analyze_pattern(frame, 95, META, BULL, 1_000_000.0)

    assert result == {
        **BASE_SIGNAL,
        "grade": grade,
        "rs_rating": 95,
        "volume_dry_up_pct": dry_up,
        "is_breakout": True,
        "breakout_price": 94.5,
        "breakout_volume_mult": volume_mult,
        "close_position_in_range": 0.9091,
        "metadata_json": {
            **BASE_SIGNAL["metadata_json"],
            "stage2": {
                "ma_50": 91.044,
                "ma_150": 83.68826200873363,
                "ma_200": 78.88246288209606,
                "high_52": 100.0,
            },
            "volume_slope": slope,
        },
    }


def _deep_first_contraction() -> pd.DataFrame:
    frame = vcp_frame()
    frame.loc[frame.index[-60 + 6], ["close", "close_ref", "high", "low"]] = [70.0] * 4
    return frame


@pytest.mark.parametrize(
    ("frame", "rs", "capital"),
    [
        (vcp_frame().tail(259), 92, 1_000_000.0),  # too little history
        (vcp_frame().iloc[::-1].reset_index(drop=True), 92, 1_000_000.0),  # not stage 2
        (vcp_frame(), 79, 1_000_000.0),  # relative strength below 80
        (vcp_frame(), 92, 100.0),  # capital too small for one share
        (_deep_first_contraction(), 92, 1_000_000.0),  # base deeper than allowed
    ],
)
def test_pattern_gates_reject(frame: pd.DataFrame, rs: int, capital: float) -> None:
    assert VCPService()._analyze_pattern(frame, rs, META, BULL, capital) is None


# --- _build_backtest_trade -------------------------------------------------------


def trade_frame(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Daily bars (open, high, low, close) starting 2025-01-01."""
    dates = [d.date() for d in pd.bdate_range("2025-01-01", periods=len(bars))]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "close_ref": [b[3] for b in bars],
        }
    )


SIGNAL = {"symbol": "VCPX", "grade": "A", "rs_rating": 92, "pivot_high": 100.0, "stop_level": 95.0}
FLAT = (100.0, 101.0, 99.0, 100.0)


@pytest.mark.parametrize(
    ("bars", "signal", "capital", "expected"),
    [
        ([FLAT] * 3, SIGNAL, 1_000_000.0, ("TRAIL_STOP_EXIT", "2025-01-03", 100.0, 2000, 0.0)),
        (
            [FLAT, (100.0, 101.0, 94.0, 95.0), FLAT],
            SIGNAL,
            1_000_000.0,
            ("STOPPED_OUT", "2025-01-02", 94.91, 2000, -10190.0),
        ),
        (
            [FLAT, (100.0, 111.0, 99.0, 110.0), (110.0, 111.0, 99.5, 100.0)],
            SIGNAL,
            1_000_000.0,
            ("BREAKEVEN_EXIT", "2025-01-03", 99.9, 2000, 9790.0),
        ),
        (
            [
                FLAT,
                (100.0, 111.0, 99.0, 110.0),
                (110.0, 111.0, 100.5, 100.6),
                (105.0, 106.0, 104.0, 105.0),
            ],
            SIGNAL,
            1_000_000.0,
            ("TRAIL_STOP_EXIT", "2025-01-06", 104.89, 2000, 14785.0),
        ),
    ],
)
def test_backtest_trade_exit_paths(
    bars: list[tuple[float, float, float, float]],
    signal: dict[str, Any],
    capital: float,
    expected: tuple[str, str, float, int, float],
) -> None:
    frame = trade_frame(bars)

    trade = VCPService()._build_backtest_trade(
        signal, frame, date(2024, 12, 31), date(2025, 1, 1), capital, 0.01
    )

    assert trade is not None
    observed = (
        trade["exit_reason"],
        trade["exit_date"],
        trade["exit_price"],
        trade["shares"],
        trade["pnl_inr"],
    )
    assert observed == expected


def test_backtest_trade_record_fields() -> None:
    frame = trade_frame([FLAT, (100.0, 101.0, 94.0, 95.0), FLAT])

    trade = VCPService()._build_backtest_trade(
        SIGNAL, frame, date(2024, 12, 31), date(2025, 1, 1), 1_000_000.0, 0.005
    )

    assert trade == {
        "symbol": "VCPX",
        "grade": "A",
        "rs_at_entry": 92,
        "entry_date": "2025-01-01",
        "entry_price": 100.0,
        "exit_date": "2025-01-02",
        "exit_price": 94.91,
        "exit_reason": "STOPPED_OUT",
        "shares": 1000,
        "pnl_inr": -5095.0,
        "pnl_pct": -5.09,
        "r_multiple": -1.02,
        "hold_days": 1,
        "regime": "BEAR",
        "status": "COMPLETED",
        "signal_date": "2024-12-31",
    }


@pytest.mark.parametrize(
    ("bars", "signal", "capital", "entry", "expected"),
    [
        ([FLAT], SIGNAL, 1_000_000.0, date(2025, 2, 3), None),  # no bar on the entry date
        (
            [(103.0, 104.0, 102.0, 103.0)],
            SIGNAL,
            1_000_000.0,
            date(2025, 1, 1),
            {"symbol": "VCPX", "entry_date": "2025-01-01", "status": "SKIPPED_GAP_UP"},
        ),
        ([FLAT], {**SIGNAL, "stop_level": 100.0}, 1_000_000.0, date(2025, 1, 1), None),
        ([FLAT], SIGNAL, 10.0, date(2025, 1, 1), None),  # capital too small for one share
    ],
)
def test_backtest_trade_entry_rejections(
    bars: list[tuple[float, float, float, float]],
    signal: dict[str, Any],
    capital: float,
    entry: date,
    expected: dict[str, Any] | None,
) -> None:
    trade = VCPService()._build_backtest_trade(
        signal, trade_frame(bars), date(2024, 12, 31), entry, capital, 0.01
    )

    assert trade == expected


# --- run_backtest -----------------------------------------------------------------

RUN_START = date(2024, 1, 1)
RUN_END = date(2024, 2, 29)


@pytest.fixture
def backtest_db() -> Generator[sessionmaker, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as session:
        session.add(IndexUniverseDefinition(id=1, index_code="NIFTY500", index_name="NIFTY 500"))
        for symbol in ("AAA", "BBB", "CCC"):
            session.add(
                IndexConstituentHistory(
                    universe_id=1,
                    symbol=symbol,
                    effective_from=date(2023, 1, 2),
                    effective_to=None,
                    import_date=date(2023, 1, 2),
                )
            )
        session.commit()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def run_golden_backtest(factory: sessionmaker, monkeypatch: pytest.MonkeyPatch) -> dict:
    """run_backtest over three symbols with fixed signals, trades and benchmark."""
    dates = [d.date() for d in pd.bdate_range("2023-01-02", RUN_END)]
    frames = pd.concat([pd.DataFrame({"symbol": s, "date": dates}) for s in ("AAA", "BBB", "CCC")])
    bench_dates = [d for d in dates if d >= RUN_START]
    benchmark = pd.DataFrame(
        {"date": bench_dates, "close": [1000.0 + 3.0 * i for i in range(len(bench_dates))]}
    )

    @contextmanager
    def scope() -> Iterator[Session]:
        db = factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    service = VCPService()
    monkeypatch.setattr(vcp_module, "db_session_scope", scope)
    monkeypatch.setattr(
        service,
        "_load_symbol_history",
        lambda symbols, **_: frames[frames["symbol"].isin(symbols)],
    )
    monkeypatch.setattr(
        service,
        "_compute_rs_map",
        lambda day_frames: {s: 80 + i for i, s in enumerate(sorted(day_frames))},
    )
    monkeypatch.setattr(
        service,
        "get_regime",
        lambda as_of=None: RegimeSnapshot(
            as_of,
            "BULL" if as_of.day % 2 else "BEAR",
            100.0,
            90.0,
            0.01 if as_of.day % 2 else 0.005,
        ),
    )

    def fake_analysis(frame: pd.DataFrame, rs_rating: int, **_: object) -> dict[str, Any]:
        last = frame["date"].max()
        return {
            "symbol": frame["symbol"].iloc[0],
            "is_breakout": (last.toordinal() + rs_rating) % 7 == 0,
            "grade": "A" if rs_rating >= 81 else "B",
            "rs_rating": rs_rating,
        }

    monkeypatch.setattr(service, "_analyze_pattern", fake_analysis)

    def fake_trade(
        signal: dict[str, Any], signal_date: date, entry_date: date, capital: float, **_: object
    ) -> dict[str, Any] | None:
        n = entry_date.toordinal()
        if n % 5 == 0:
            return None
        if n % 5 == 1:
            return {"symbol": signal["symbol"], "status": "SKIPPED_GAP_UP"}
        hold = n % 4 + 1
        exit_date = pd.Timestamp(entry_date) + pd.offsets.BDay(hold)
        pnl = round(((n % 9) - 4) * 0.001 * capital, 2)
        return {
            "symbol": signal["symbol"],
            "pnl_inr": pnl,
            "hold_days": hold,
            "r_multiple": round(pnl / 1000.0, 2),
            "entry_date": entry_date.isoformat(),
            "exit_date": exit_date.date().isoformat(),
            "signal_date": signal_date.isoformat(),
        }

    monkeypatch.setattr(service, "_build_backtest_trade", fake_trade)
    monkeypatch.setattr(
        vcp_module.daily_history_service, "load_index_history", lambda *a, **k: benchmark.copy()
    )
    return service.run_backtest(
        {
            "universe": "NIFTY500",
            "start_date": RUN_START.isoformat(),
            "end_date": RUN_END.isoformat(),
            "initial_capital": 500_000,
        }
    )


def test_run_backtest_matches_golden_output(
    backtest_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    response = run_golden_backtest(backtest_db, monkeypatch)

    assert response["status"] == "completed"
    assert response["run_id"].startswith("vcp-")
    assert json.loads(json.dumps(response["result"], default=str)) == golden


def test_run_backtest_without_trading_dates_fails(
    backtest_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        db = backtest_db()
        try:
            yield db
        finally:
            db.close()

    service = VCPService()
    monkeypatch.setattr(vcp_module, "db_session_scope", scope)
    monkeypatch.setattr(
        service,
        "_load_symbol_history",
        lambda symbols, **_: pd.DataFrame({"symbol": ["AAA"], "date": [date(2023, 1, 2)]}),
    )

    with pytest.raises(ValueError) as excinfo:
        service.run_backtest(
            {"universe": "NIFTY500", "start_date": "2024-01-01", "end_date": "2024-01-31"}
        )

    assert str(excinfo.value) == "No trading dates available in the selected range"
