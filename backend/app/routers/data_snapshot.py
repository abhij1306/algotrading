"""
Phase-1 snapshot read APIs backed by curated parquet artifacts.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import pyarrow.dataset as ds
except ModuleNotFoundError:
    ds = None
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import DatasetRun, get_db
from ..services.symbol_master import symbol_master

router = APIRouter(prefix="/api/data", tags=["Data Snapshots"])
DB_DEPENDENCY = Depends(get_db)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PHASE1_ROOT = PROJECT_ROOT / "data_system"
CURATED_ROOT = PHASE1_ROOT / "04_curated" / "phase1"
METADATA_ROOT = PHASE1_ROOT / "05_metadata" / "phase1"


def _require_file(path: Path) -> None:
    if ds is None:
        raise HTTPException(
            status_code=503,
            detail="pyarrow is not installed. Install backend requirements to enable snapshot APIs.",
        )
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Phase-1 artifact not available: {path}",
        )


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        detail = f"Invalid date '{value}', expected YYYY-MM-DD"
        raise HTTPException(status_code=400, detail=detail) from exc


@router.get("/snapshot/stock")
def get_stock_snapshot(
    date: str = Query(..., description="Snapshot date (YYYY-MM-DD)"),
    symbol: str = Query(..., description="Stock symbol"),
) -> dict[str, Any]:
    target_date = _parse_date(date)
    normalized_symbol = symbol_master.to_db(symbol)

    path = CURATED_ROOT / "snapshot_stock_daily.parquet"
    _require_file(path)

    dataset = ds.dataset(path)
    table = dataset.to_table(
        filter=(ds.field("date") == target_date.isoformat())
        & (ds.field("symbol") == normalized_symbol)
    )
    rows = table.to_pylist()
    if not rows:
        detail = "Snapshot row not found for requested date/symbol"
        raise HTTPException(status_code=404, detail=detail)

    return {
        "date": target_date.isoformat(),
        "symbol": normalized_symbol,
        "row": rows[0],
        "artifact": str(path),
    }


@router.get("/snapshot/universe")
def get_universe_snapshot(
    date: str = Query(..., description="Snapshot date (YYYY-MM-DD)"),
    universe: str = Query("NIFTY50", description="Universe identifier"),
) -> dict[str, Any]:
    target_date = _parse_date(date)
    universe_id = universe.strip().upper()
    if universe_id != "NIFTY50":
        detail = "Phase-1 currently supports universe=NIFTY50 only"
        raise HTTPException(status_code=400, detail=detail)

    path = CURATED_ROOT / "snapshot_nifty50_daily.parquet"
    _require_file(path)

    dataset = ds.dataset(path)
    table = dataset.to_table(filter=ds.field("date") == target_date.isoformat())
    rows = table.to_pylist()
    if not rows:
        detail = "No universe snapshot rows found for requested date"
        raise HTTPException(status_code=404, detail=detail)

    return {
        "date": target_date.isoformat(),
        "universe": universe_id,
        "count": len(rows),
        "rows": rows,
        "artifact": str(path),
    }


@router.get("/snapshot/status")
def get_snapshot_status(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    status = {
        "phase1_root_exists": PHASE1_ROOT.exists(),
        "curated": {},
        "metadata_files": {},
        "latest_run_log": None,
        "latest_db_run": None,
    }

    for artifact in [
        "equity_ohlcv.parquet",
        "equity_ohlcv_adj.parquet",
        "nifty50_weights_monthly.parquet",
        "nifty50_membership_daily.parquet",
        "snapshot_stock_daily.parquet",
        "snapshot_nifty50_daily.parquet",
    ]:
        path = CURATED_ROOT / artifact
        status["curated"][artifact] = {
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    for filename in [
        "source_manifest.json",
        "data_contract.json",
        "checksums.json",
        "run_log.jsonl",
    ]:
        path = METADATA_ROOT / filename
        status["metadata_files"][filename] = {
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    run_log = METADATA_ROOT / "run_log.jsonl"
    if run_log.exists():
        lines = [
            line.strip()
            for line in run_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if lines:
            status["latest_run_log"] = json.loads(lines[-1])

    latest = db.query(DatasetRun).order_by(DatasetRun.started_at.desc()).first()
    if latest:
        status["latest_db_run"] = {
            "run_id": latest.run_id,
            "asof_date": latest.asof_date.isoformat(),
            "mode": latest.mode,
            "status": latest.status,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
        }

    return status
