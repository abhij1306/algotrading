"""Characterization tests for scripts/load_bhavcopy.py against the test database."""

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Company, HistoricalPrice

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_bhavcopy.py"

HEADER = (
    "SYMBOL, SERIES, DATE1, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, "
    "TTL_TRD_QNTY, DELIV_QTY, DELIV_PER\n"
)
CSV = (
    HEADER
    + """BHVOLD, EQ, 13-Feb-2026, 10, 12, 9, 11, 1000, 400, 40.5
BHVNEW, EQ, 13-Feb-2026, 20, 22, 19, 21,,,
BHVBE, BE, 13-Feb-2026, 1, 1, 1, 1, 1, 1, 1
BHVBAD, EQ, 13-Feb-2026, x, 1, 1, 1, 1, 1, 1
"""
)


@pytest.fixture
def script(test_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("load_bhavcopy_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SessionLocal", sessionmaker(bind=test_engine))
    return module


def _prices(session: Session, symbol: str) -> list[tuple]:
    company = session.query(Company).filter(Company.symbol == symbol).one()
    return [
        (p.date, p.open, p.high, p.low, p.close, p.volume, p.deliverable_qty, p.delivery_pct)
        for p in session.query(HistoricalPrice).filter(HistoricalPrice.company_id == company.id)
    ]


def test_load_bhavcopy_inserts_then_updates(
    script: ModuleType, db_session: Session, tmp_path: Path
) -> None:
    db_session.add(Company(symbol="BHVOLD", is_active=False))
    db_session.commit()
    csv_path = tmp_path / "sec_bhavdata_full_13022026.csv"
    csv_path.write_text(CSV, encoding="utf-8")

    first = script.load_bhavcopy(str(csv_path))

    assert {k: v for k, v in first.items() if k != "errors"} == {
        "total": 3,
        "success": 2,
        "skipped": 0,
        "failed": 1,
        "added": 2,
        "activated": 3,
    }
    assert first["errors"] == ["BHVBAD: could not convert string to float: ' x'"]
    db_session.expire_all()
    assert _prices(db_session, "BHVOLD") == [
        (date(2026, 2, 13), 10.0, 12.0, 9.0, 11.0, 1000, 400, 40.5)
    ]
    assert _prices(db_session, "BHVNEW") == [(date(2026, 2, 13), 20.0, 22.0, 19.0, 21.0, 0, 0, 0)]
    assert db_session.query(Company).filter(Company.symbol == "BHVOLD").one().is_active is True
    assert db_session.query(Company).filter(Company.symbol == "BHVBE").first() is None

    csv_path.write_text(CSV.replace("11, 1000, 400, 40.5", "15, 2000, 500, 25"), encoding="utf-8")
    second = script.load_bhavcopy(str(csv_path), target_date=date(2026, 2, 13))

    assert (second["success"], second["failed"], "added" in second) == (2, 1, False)
    db_session.expire_all()
    assert _prices(db_session, "BHVOLD") == [
        (date(2026, 2, 13), 10.0, 12.0, 9.0, 15.0, 2000, 500, 25.0)
    ]


def test_parse_date_from_filename(script: ModuleType) -> None:
    assert script.parse_date_from_filename("x/sec_bhavdata_full_01032026.csv") == date(2026, 3, 1)
    assert script.parse_date_from_filename("weird.csv") == date.today()
