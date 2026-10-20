"""VCP scan through ScanService with fake history and regime ports.

The table tests touch no database or network; the last test stores a scan in in-memory
SQLite to check point-in-time membership and the queue/cancel round trip.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.base import Base
from app.models import IndexConstituentHistory, IndexUniverseDefinition, VCPScanResult
from app.services.vcp.config import VCPParams
from app.services.vcp.regime import RegimeSnapshot
from app.services.vcp.scan import ScanService, rank_signals, serialize_scan_row
from app.utils.clock import FixedClock

DATES = [d.date() for d in pd.bdate_range("2024-01-01", periods=301)]
BASE_DAY = DATES[299]  # last bar of the base: 2025-02-21
BREAKOUT_DAY = DATES[300]  # the breakout bar: 2025-02-24
META = {"VCPX": {"name": "VCP X", "sector": "Tech"}}


def _ramp(points: list[tuple[int, float]]) -> list[float]:
    """Straight lines between (bar, price) points."""
    prices: list[float] = []
    for (start, low), (end, high) in zip(points, points[1:], strict=False):
        prices.extend(np.linspace(low, high, end - start + 1)[:-1])
    prices.append(points[-1][1])
    return prices


# A stage-2 uptrend, then a 60-bar base with three tightening pullbacks on drying volume,
# then one breakout bar (close 94.5 over the 93.46 pivot on heavy volume).
VCP_CLOSES = (
    _ramp([(0, 40.0), (229, 85.0)])
    + _ramp([(0, 86.5), (9, 98.5)])
    + _ramp([(0, 100.0), (6, 82.0), (12, 96.0), (17, 88.3), (22, 93.0), (26, 90.2), (59, 91.0)])
    + [94.5]
)
VCP_VOLUME = [1e6] * 240 + list(np.linspace(9e5, 5e5, 22)) + [8e4] * 5 + [1.2e5] * 32 + [2e5, 6e5]


def bars(symbol: str, closes: list[float], volume: list[float]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "symbol": symbol,
            "date": DATES[: len(closes)],
            "open": closes,
            "close": closes,
            "close_ref": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "volume": volume,
        }
    )
    if len(closes) == len(VCP_CLOSES):  # a wide-range breakout bar closing near its high
        frame.loc[frame.index[-1], ["high", "low"]] = [94.5 * 1.002, 94.5 * 0.98]
    return frame


VCPX = bars("VCPX", VCP_CLOSES, VCP_VOLUME)
LAGGARD = bars("LAGG", list(np.linspace(100.0, 50.0, 300)), [1e6] * 300)


class FakeHistory:
    """Equity bars filtered by symbol and date range, like the canonical history service."""

    def __init__(self, *frames: pd.DataFrame) -> None:
        self.frame = pd.concat(frames, ignore_index=True)

    def load_equity_history(
        self, symbols: list[str], start_date: date | None = None, end_date: date | None = None
    ) -> pd.DataFrame:
        frame = self.frame[self.frame["symbol"].isin(symbols)]
        if start_date is not None:
            frame = frame[frame["date"] >= start_date]
        if end_date is not None:
            frame = frame[frame["date"] <= end_date]
        return frame.reset_index(drop=True)

    def latest_equity_date(self) -> date | None:
        return None if self.frame.empty else self.frame["date"].max()


class FakeRegime:
    def __init__(self, regime: str = "BULL", risk_pct: float = 0.01) -> None:
        self.regime = regime
        self.risk_pct = risk_pct

    def get_regime(self, as_of: date | None = None) -> RegimeSnapshot:
        return RegimeSnapshot(as_of or BASE_DAY, self.regime, 100.0, 90.0, self.risk_pct)


def scan_service(history: FakeHistory, regime: FakeRegime | None = None) -> ScanService:
    return ScanService(history, regime or FakeRegime())


def summary(signal: dict[str, object]) -> tuple[object, ...]:
    return (
        signal["symbol"],
        signal["company_name"],
        signal["grade"],
        signal["rs_rating"],
        signal["is_breakout"],
        signal["breakout_price"],
        signal["planned_shares"],
        signal["regime"],
    )


@pytest.mark.parametrize(
    ("history", "symbols", "scan_date", "regime", "scanned", "expected"),
    [
        pytest.param(
            FakeHistory(VCPX, LAGGARD),
            ["VCPX", "LAGG"],
            BASE_DAY,
            FakeRegime(),
            2,
            [("VCPX", "VCP X", "A", 99, False, None, 2139, "BULL")],
            id="vcp-base-signals-laggard-does-not",
        ),
        pytest.param(
            FakeHistory(VCPX, LAGGARD),
            ["VCPX", "LAGG"],
            BREAKOUT_DAY,
            FakeRegime(),
            1,
            [("VCPX", "VCP X", "A", 99, True, 94.5, 2139, "BULL")],
            id="breakout-day-is-a-breakout-laggard-is-stale",
        ),
        pytest.param(
            FakeHistory(VCPX),
            ["VCPX"],
            BASE_DAY,
            FakeRegime("BEAR", 0.005),
            1,
            [("VCPX", "VCP X", "A", 99, False, None, 1314, "BEAR")],
            id="bear-regime-halves-the-risk",
        ),
        pytest.param(
            FakeHistory(VCPX.head(259)),
            ["VCPX"],
            DATES[258],
            FakeRegime(),
            0,
            [],
            id="too-little-history-is-not-scanned",
        ),
        pytest.param(
            FakeHistory(VCPX.head(299)),
            ["VCPX"],
            BASE_DAY,
            FakeRegime(),
            0,
            [],
            id="no-bar-on-the-scan-date-is-not-scanned",
        ),
        pytest.param(
            FakeHistory(VCPX, LAGGARD),
            ["LAGG"],
            BASE_DAY,
            FakeRegime(),
            1,
            [],
            id="non-members-are-not-scanned",
        ),
    ],
)
def test_scan_symbols(
    history: FakeHistory,
    symbols: list[str],
    scan_date: date,
    regime: FakeRegime,
    scanned: int,
    expected: list[tuple[object, ...]],
) -> None:
    outcome = scan_service(history, regime).scan_symbols(
        symbols, scan_date, 1_000_000.0, lambda found: {s: META[s] for s in found if s in META}
    )

    assert outcome.symbols_scanned == scanned
    summaries = [summary(signal) for signal in outcome.signals]
    assert summaries == expected
    assert outcome.regime.scan_date == scan_date


def test_scan_on_the_base_day_never_reads_the_breakout_bar() -> None:
    """Scanning the day before the breakout must match a history that ends on that day."""
    service_with_future = scan_service(FakeHistory(VCPX))
    service_without_future = scan_service(FakeHistory(VCPX.head(300)))

    def meta(_found: list[str]) -> dict[str, dict[str, str]]:
        return {}

    with_future = service_with_future.scan_symbols(["VCPX"], BASE_DAY, 1_000_000.0, meta)
    without_future = service_without_future.scan_symbols(["VCPX"], BASE_DAY, 1_000_000.0, meta)

    seen = [(s["company_name"], s["sector"], s["pivot_high"]) for s in with_future.signals]
    assert with_future.signals == without_future.signals
    assert seen == [("VCPX", "Unknown", 93.46)]


@pytest.mark.parametrize(
    ("history", "requested", "expected"),
    [
        pytest.param(FakeHistory(VCPX), date(2025, 1, 6), date(2025, 1, 6), id="requested"),
        pytest.param(FakeHistory(VCPX), None, BREAKOUT_DAY, id="latest-history-date"),
    ],
)
def test_as_of_date(history: FakeHistory, requested: date | None, expected: date) -> None:
    assert scan_service(history).as_of_date(requested) == expected


def test_as_of_date_without_history_fails() -> None:
    service = scan_service(FakeHistory(VCPX.head(0)))

    with pytest.raises(ValueError) as excinfo:
        service.as_of_date()

    assert str(excinfo.value) == "Canonical daily equity history is empty"


def _signal(symbol: str, grade: str, breakout: bool, depth: float, rs: int) -> dict[str, object]:
    return {
        "symbol": symbol,
        "grade": grade,
        "is_breakout": breakout,
        "final_contraction_depth": depth,
        "rs_rating": rs,
    }


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        pytest.param(
            [_signal("B1", "B", True, 2.0, 99), _signal("A1", "A", False, 9.0, 80)],
            ["A1", "B1"],
            id="grade-before-breakout",
        ),
        pytest.param(
            [_signal("BASE", "A", False, 1.0, 99), _signal("BRK", "A", True, 9.0, 80)],
            ["BRK", "BASE"],
            id="breakout-before-depth",
        ),
        pytest.param(
            [_signal("WIDE", "A", True, 6.0, 99), _signal("TIGHT", "A", True, 3.0, 80)],
            ["TIGHT", "WIDE"],
            id="tighter-contraction-first",
        ),
        pytest.param(
            [_signal("LOW", "A", True, 3.0, 85), _signal("HIGH", "A", True, 3.0, 95)],
            ["HIGH", "LOW"],
            id="higher-rs-first",
        ),
        pytest.param(
            [_signal("X", "D", True, 1.0, 99), _signal("C1", "C", False, 9.0, 80)],
            ["C1", "X"],
            id="unknown-grade-last",
        ),
    ],
)
def test_rank_signals(signals: list[dict[str, object]], expected: list[str]) -> None:
    order = [signal["symbol"] for signal in rank_signals(signals)]
    assert order == expected


def _row(stop_level: float, metadata: dict[str, object] | None) -> VCPScanResult:
    return VCPScanResult(
        id=7,
        scan_date=date(2025, 2, 21),
        symbol="VCPX",
        grade="A",
        rs_rating=92,
        pivot_high=100.0,
        stop_level=stop_level,
        regime="BULL",
        signal_status="SIGNAL",
        metadata_json=metadata,
    )


@pytest.mark.parametrize(
    ("row", "params", "expected"),
    [
        pytest.param(
            _row(95.0, {"risk_pct": 0.01}),
            VCPParams(),
            (2000, 200000.0, 10000.0, 110.0),
            id="bull-risk-hits-the-position-cap",
        ),
        pytest.param(
            _row(95.0, {"risk_pct": 0.005}),
            VCPParams(),
            (1000, 100000.0, 5000.0, 110.0),
            id="bear-risk-halves-the-size",
        ),
        pytest.param(
            _row(90.0, None),
            VCPParams(),
            (1000, 100000.0, 10000.0, 120.0),
            id="missing-metadata-uses-bull-risk",
        ),
        pytest.param(
            _row(101.0, {"risk_pct": 0.01}),
            VCPParams(),
            (2000, 200000.0, 20.0, 100.02),
            id="stop-above-pivot-uses-minimum-risk",
        ),
        pytest.param(
            _row(95.0, {"risk_pct": 0.01}),
            VCPParams(default_capital=100_000.0),
            (200, 20000.0, 1000.0, 110.0),
            id="sized-on-params-capital",
        ),
    ],
)
def test_serialize_scan_row_sizing(
    row: VCPScanResult, params: VCPParams, expected: tuple[int, float, float, float]
) -> None:
    signal = serialize_scan_row(row, params)

    sizing = (
        signal["planned_shares"],
        signal["planned_position_value"],
        signal["capital_risk"],
        signal["two_r_target"],
    )
    labels = (signal["company_name"], signal["sector"], signal["planned_entry"])
    assert sizing == expected
    assert labels == ("VCPX", "Unknown", 100.0)


# --- stored scan: point-in-time members, queue and cancel -----------------------------


@pytest.fixture
def factory() -> Generator[sessionmaker, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with session_factory() as db:
        db.add(IndexUniverseDefinition(id=1, index_code="NIFTY500", index_name="NIFTY 500"))
        for symbol, start, end in [
            ("VCPX", date(2024, 1, 1), None),
            ("GONE", date(2024, 1, 1), date(2025, 1, 31)),  # left before the scan date
            ("LATE", date(2025, 3, 3), None),  # joins after the scan date
        ]:
            db.add(
                IndexConstituentHistory(
                    universe_id=1,
                    symbol=symbol,
                    effective_from=start,
                    effective_to=end,
                    import_date=start,
                )
            )
        db.commit()
    try:
        yield session_factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_run_scan_stores_point_in_time_signals_and_queues_them(factory: sessionmaker) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        with factory() as db:
            yield db

    # GONE and LATE carry the same VCP setup, so only membership can keep them out.
    history = FakeHistory(*(VCPX.assign(symbol=symbol) for symbol in ("VCPX", "GONE", "LATE")))
    clock = FixedClock(datetime(2025, 2, 21, 11, 0, tzinfo=UTC))
    service = ScanService(history, FakeRegime(), session_scope=scope, clock=clock)

    scan = service.run_scan("nifty500", BASE_DAY)
    [signal] = scan["results"]
    queued = service.queue_signal(signal["id"])
    cancelled = service.cancel_signal(signal["id"])

    scope_fields = (scan["universe"], scan["constituent_mode"], scan["total_symbols_scanned"])
    assert scope_fields == ("NIFTY500", "bias_corrected", 1)
    assert scan["scan_timestamp"] == "2025-02-21T11:00:00+00:00"
    assert scan["regime"]["regime"] == "BULL"
    stored = (signal["symbol"], signal["grade"], signal["signal_status"])
    assert stored == ("VCPX", "A", "SIGNAL")
    assert queued["signal_status"] == "PENDING_ENTRY"
    assert cancelled["signal_status"] == "SIGNAL"
    latest = service.get_latest_scan("NIFTY500")
    latest_symbols = [row["symbol"] for row in latest["results"]]
    assert latest["scan_id"] == scan["scan_id"]
    assert latest_symbols == ["VCPX"]


def test_queue_unknown_signal_fails(factory: sessionmaker) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        with factory() as db:
            yield db

    service = ScanService(FakeHistory(VCPX), FakeRegime(), session_scope=scope)

    with pytest.raises(ValueError) as excinfo:
        service.queue_signal(404)

    assert str(excinfo.value) == "Signal not found"
