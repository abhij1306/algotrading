"""Characterization tests for ensure_strategy_schema on legacy SQLite databases."""

from pathlib import Path

import pytest
from sqlalchemy import Column, Engine, Float, MetaData, Table, create_engine, inspect, text

from app.database import ensure_strategy_schema
from app.models.vcp import StrategyPosition, VCPScanResult


def _legacy_engine(tmp_path: Path, drop: dict[str, set[str]], extra_pnl_rs: bool) -> Engine:
    """Engine whose vcp_scan_results/positions tables lack the `drop` columns."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    metadata = MetaData()
    for model_table in (VCPScanResult.__table__, StrategyPosition.__table__):
        columns = [
            column._copy()
            for column in model_table.columns
            if column.name not in drop.get(model_table.name, set())
        ]
        if model_table.name == "positions" and extra_pnl_rs:
            columns.append(Column("pnl_rs", Float))
        Table(model_table.name, metadata, *columns)
    metadata.create_all(engine)
    return engine


def _columns(engine: Engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _insert(engine: Engine, table_name: str, values: dict[str, object]) -> None:
    """Insert raw values (untyped columns, so SQLite stores the literals as given)."""
    table = Table(table_name, MetaData(), *(Column(name) for name in values))
    with engine.begin() as connection:
        connection.execute(table.insert(), values)


def _insert_position(engine: Engine, pnl_rs: bool) -> None:
    values: dict[str, object] = {
        "symbol": "SBIN",
        "entry_date": "2026-01-02",
        "entry_price": 10,
        "stop_price": 9,
        "shares": 1,
        "shares_remaining": 1,
        "two_r_price": 12,
        "two_r_hit": 0,
        "regime_at_entry": "BULL",
        "risk_pct_at_entry": 1,
        "status": "OPEN",
        "is_paper": 1,
        "created_at": "2026-01-02",
        "updated_at": "2026-01-02",
    }
    if pnl_rs:
        values["pnl_rs"] = 125.5
    _insert(engine, "positions", values)


def _pnl_inr(engine: Engine) -> list[float | None]:
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text("SELECT pnl_inr FROM positions"))]


def test_legacy_pnl_rs_is_copied_when_no_columns_are_missing(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path, {}, extra_pnl_rs=True)
    _insert_position(engine, pnl_rs=True)

    ensure_strategy_schema(bind=engine)

    assert _pnl_inr(engine) == [125.5]


def test_missing_pnl_inr_is_added_and_backfilled(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path, {"positions": {"pnl_inr", "notes"}}, extra_pnl_rs=True)
    _insert_position(engine, pnl_rs=True)

    ensure_strategy_schema(bind=engine)

    assert {"pnl_inr", "notes"} <= _columns(engine, "positions")
    assert _pnl_inr(engine) == [125.5]


def test_missing_positions_columns_without_pnl_rs(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path, {"positions": {"pnl_inr"}}, extra_pnl_rs=False)
    _insert_position(engine, pnl_rs=False)

    ensure_strategy_schema(bind=engine)

    assert _pnl_inr(engine) == [None]


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ({"scan_id", "scan_timestamp"}, ("7", "2026-03-04 10:00:00")),
        ({"scan_id"}, ("7", "2026-01-01 09:00:00")),
        ({"metadata_json"}, ("abc", "2026-01-01 09:00:00")),
    ],
)
def test_vcp_scan_results_backfill(
    tmp_path: Path, dropped: set[str], expected: tuple[str, str]
) -> None:
    engine = _legacy_engine(tmp_path, {"vcp_scan_results": dropped}, extra_pnl_rs=False)
    values = {
        "id": 7,
        "scan_date": "2026-03-04",
        "universe": "NIFTY50",
        "symbol": "SBIN",
        "grade": "A",
        "rs_rating": 90,
        "stage2_conditions_met": 8,
        "contraction_count": 3,
        "contraction_depths": "[]",
        "final_contraction_depth": 5.0,
        "volume_dry_up_pct": 40.0,
        "pivot_high": 100.0,
        "stop_level": 95.0,
        "stop_pct": 5.0,
        "is_breakout": 0,
        "overhead_clear": 1,
        "regime": "BULL",
        "signal_status": "SIGNAL",
        "created_at": "2026-03-04 10:00:00",
        "scan_id": "abc",
        "scan_timestamp": "2026-01-01 09:00:00",
        "metadata_json": "{}",
    }
    for name in dropped:
        values.pop(name)
    _insert(engine, "vcp_scan_results", values)

    ensure_strategy_schema(bind=engine)

    assert dropped <= _columns(engine, "vcp_scan_results")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT scan_id, scan_timestamp FROM vcp_scan_results")).one()
    assert tuple(row) == expected


def test_missing_tables_are_skipped(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")

    ensure_strategy_schema(bind=engine)

    assert inspect(engine).get_table_names() == []
