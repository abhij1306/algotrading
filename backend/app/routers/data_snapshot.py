"""
Phase-1 snapshot read APIs backed by curated parquet artifacts.
"""

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any

try:
    import pyarrow.dataset as ds
except ModuleNotFoundError:
    ds = None
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import DatasetRun, get_db
from ..paths import CURATED_ROOT, DATA_ROOT, METADATA_ROOT
from ..services.symbol_master import symbol_master
from ..utils.dates import parse_iso_date

router = APIRouter(prefix="/api/data", tags=["Data Snapshots"])
DBSession = Annotated[Session, Depends(get_db)]
logger = logging.getLogger(__name__)

SNAPSHOT_RESPONSES = {
    400: {"description": "Invalid request parameters"},
    404: {"description": "Requested snapshot data was not found"},
    503: {"description": "Snapshot dependencies or artifacts are not available"},
}


def _artifact_status(path: Path) -> dict[str, int | bool]:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {"exists": False, "size_bytes": 0}
    return {"exists": True, "size_bytes": stat_result.st_size}


def _collect_artifact_status(
    root: Path, filenames: Iterable[str]
) -> dict[str, dict[str, int | bool]]:
    return {filename: _artifact_status(root / filename) for filename in filenames}


def _read_latest_run_log() -> dict[str, Any] | None:
    run_log = METADATA_ROOT / "run_log.jsonl"
    if not run_log.exists():
        return None

    lines = [
        line.strip() for line in run_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for idx in range(len(lines) - 1, -1, -1):
        try:
            return json.loads(lines[idx])
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed run_log.jsonl line at index %s: %s", idx, exc)
    return None


def _serialize_latest_db_run(db: Session) -> dict[str, Any] | None:
    latest = db.query(DatasetRun).order_by(DatasetRun.started_at.desc()).first()
    if not latest:
        return None
    return {
        "run_id": latest.run_id,
        "asof_date": latest.asof_date.isoformat(),
        "mode": latest.mode,
        "status": latest.status,
        "started_at": latest.started_at.isoformat() if latest.started_at else None,
        "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
    }


def _require_file(path: Path) -> str | None:
    if ds is None:
        return "pyarrow is not installed. Install backend requirements to enable snapshot APIs."
    if not path.exists():
        return f"Phase-1 artifact not available: {path}"
    return None


@router.get(
    "/snapshot/stock",
    responses=SNAPSHOT_RESPONSES,
)
def get_stock_snapshot(
    date: Annotated[str, Query(description="Snapshot date (YYYY-MM-DD)")],
    symbol: Annotated[str, Query(description="Stock symbol")],
) -> dict[str, Any]:
    target_date = parse_iso_date(date, field="date")
    normalized_symbol = symbol_master.to_db(symbol)

    path = CURATED_ROOT / "snapshot_stock_daily.parquet"
    artifact_error = _require_file(path)
    if artifact_error:
        raise HTTPException(status_code=503, detail=artifact_error)

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


@router.get(
    "/snapshot/universe",
    responses=SNAPSHOT_RESPONSES,
)
def get_universe_snapshot(
    date: Annotated[str, Query(description="Snapshot date (YYYY-MM-DD)")],
    universe: Annotated[str, Query(description="Universe identifier")] = "NIFTY50",
) -> dict[str, Any]:
    target_date = parse_iso_date(date, field="date")
    universe_id = universe.strip().upper()
    if universe_id != "NIFTY50":
        detail = "Phase-1 currently supports universe=NIFTY50 only"
        raise HTTPException(status_code=400, detail=detail)

    path = CURATED_ROOT / "snapshot_nifty50_daily.parquet"
    artifact_error = _require_file(path)
    if artifact_error:
        raise HTTPException(status_code=503, detail=artifact_error)

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
def get_snapshot_status(db: DBSession) -> dict[str, Any]:
    status = {
        "phase1_root_exists": DATA_ROOT.exists(),
        "curated": _collect_artifact_status(
            CURATED_ROOT,
            [
                "equity_ohlcv.parquet",
                "equity_ohlcv_adj.parquet",
                "nifty50_weights_monthly.parquet",
                "nifty50_membership_daily.parquet",
                "snapshot_stock_daily.parquet",
                "snapshot_nifty50_daily.parquet",
            ],
        ),
        "metadata_files": _collect_artifact_status(
            METADATA_ROOT,
            [
                "source_manifest.json",
                "data_contract.json",
                "checksums.json",
                "run_log.jsonl",
            ],
        ),
        "latest_run_log": _read_latest_run_log(),
        "latest_db_run": _serialize_latest_db_run(db),
    }

    return status
