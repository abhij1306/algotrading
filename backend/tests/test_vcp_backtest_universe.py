from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.base import Base
from app.models import IndexConstituentHistory, IndexUniverseDefinition
from app.services import vcp_service as vcp_module
from app.services.vcp_service import RegimeSnapshot, VCPService

HISTORY_START = date(2023, 1, 2)
JOIN_DATE = date(2024, 3, 1)  # LATER_JOINER enters the index here
LEAVE_DATE = date(2024, 2, 1)  # DROPPED stops being a member the day before this
RUN_START = date(2024, 1, 1)
RUN_END = date(2024, 4, 30)
TRADE_PNL = 10.0  # every fake trade realises this on its entry day


@pytest.fixture
def session_factory() -> Generator[sessionmaker, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = factory()
    session.add(IndexUniverseDefinition(id=1, index_code="NIFTY500", index_name="NIFTY 500"))
    # Earliest record predates the run, so every run date is resolved point-in-time.
    rows = [
        ("STABLE", HISTORY_START, None),
        ("DROPPED", HISTORY_START, date(2024, 1, 31)),
        ("LATER_JOINER", JOIN_DATE, None),
    ]
    for symbol, start, end in rows:
        session.add(
            IndexConstituentHistory(
                universe_id=1,
                symbol=symbol,
                effective_from=start,
                effective_to=end,
                import_date=start,
            )
        )
    session.commit()
    session.close()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_backtest_membership_is_point_in_time(
    session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = [d.date() for d in pd.bdate_range(HISTORY_START, RUN_END)]
    frames = pd.concat(
        [
            pd.DataFrame({"symbol": symbol, "date": dates})
            for symbol in ("STABLE", "DROPPED", "LATER_JOINER")
        ]
    )

    @contextmanager
    def scope() -> Generator:
        db = session_factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    signals: list[tuple[str, date]] = []
    entries: list[tuple[str, date]] = []
    service = VCPService()
    monkeypatch.setattr(vcp_module, "db_session_scope", scope)
    # Like the real loader, only return the symbols that were asked for.
    monkeypatch.setattr(
        service,
        "_load_symbol_history",
        lambda symbols, **_: frames[frames["symbol"].isin(symbols)],
    )
    monkeypatch.setattr(
        service, "_compute_rs_map", lambda day_frames: dict.fromkeys(day_frames, 90)
    )
    monkeypatch.setattr(
        service,
        "get_regime",
        lambda as_of=None: RegimeSnapshot(as_of, "BULL", 100.0, 90.0, 0.01),
    )
    monkeypatch.setattr(
        service,
        "_analyze_pattern",
        lambda frame, **_: {
            "symbol": frame["symbol"].iloc[0],
            "is_breakout": True,
            "grade": "A",
            "rs_rating": 90,
        },
    )

    def fake_trade(
        signal: dict[str, Any], signal_date: date, entry_date: date, **_: object
    ) -> dict[str, Any]:
        signals.append((signal["symbol"], signal_date))
        entries.append((signal["symbol"], entry_date))
        return {
            "symbol": signal["symbol"],
            "pnl_inr": TRADE_PNL,
            "hold_days": 1,
            "r_multiple": 0.0,
            "entry_date": entry_date.isoformat(),
            "exit_date": entry_date.isoformat(),
        }

    monkeypatch.setattr(service, "_build_backtest_trade", fake_trade)
    monkeypatch.setattr(
        vcp_module.daily_history_service,
        "load_index_history",
        lambda *a, **k: pd.DataFrame(columns=["date", "close"]),
    )

    result = service.run_backtest(
        {
            "universe": "NIFTY500",
            "start_date": RUN_START.isoformat(),
            "end_date": RUN_END.isoformat(),
        }
    )["result"]

    dropped = [d for s, d in signals if s == "DROPPED"]
    joiner = [d for s, d in signals if s == "LATER_JOINER"]
    # Removed name is still traded before it left, never after.
    assert dropped
    assert max(dropped) < LEAVE_DATE
    # Trades enter the next session, which must also fall inside membership.
    assert max(d for s, d in entries if s == "DROPPED") < LEAVE_DATE
    # Late joiner is never traded before it joined, but is traded afterwards.
    assert joiner
    assert min(joiner) >= JOIN_DATE
    assert result["methodology"]["survivorship_mode"] == "bias_corrected"
    assert result["metrics"]["survivorship_bias_label"] == "BIAS-CORRECTED"
    # Equity must include exits realised on the final trading date.
    metrics = result["metrics"]
    assert metrics["final_equity"] == round(
        metrics["initial_capital"] + TRADE_PNL * len(signals), 2
    )
    assert result["equity_curve"][-1]["date"] == RUN_END.isoformat()
