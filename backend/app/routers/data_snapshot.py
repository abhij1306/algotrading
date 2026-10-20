"""
Phase-1 snapshot read APIs backed by curated parquet artifacts.
"""

import logging
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DatasetRun
from ..paths import DATA_ROOT
from ..services.snapshots import (
    SnapshotCorruptError,
    SnapshotDateNotFoundError,
    SnapshotError,
    SnapshotKind,
    SnapshotStore,
    SnapshotUnavailableError,
    get_snapshot_store,
    to_records,
)
from ..services.symbol_master import symbol_master
from ..utils.dates import parse_iso_date

router = APIRouter(prefix="/api/data", tags=["Data Snapshots"])
DBSession = Annotated[Session, Depends(get_db)]
Store = Annotated[SnapshotStore, Depends(get_snapshot_store)]
logger = logging.getLogger(__name__)

SNAPSHOT_RESPONSES = {
    400: {"description": "Invalid request parameters"},
    404: {"description": "Requested snapshot data was not found"},
    503: {"description": "Snapshot dependencies or artifacts are not available"},
}


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


def _raise_http(exc: SnapshotError, not_found_detail: str) -> NoReturn:
    """Map a store failure to the endpoint's status code; the store's messages are safe."""
    if isinstance(exc, SnapshotDateNotFoundError):
        raise HTTPException(status_code=404, detail=not_found_detail) from exc
    if isinstance(exc, (SnapshotUnavailableError, SnapshotCorruptError)):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=503, detail="Phase-1 snapshot is not available") from exc


@router.get(
    "/snapshot/stock",
    responses=SNAPSHOT_RESPONSES,
)
def get_stock_snapshot(
    date: Annotated[str, Query(description="Snapshot date (YYYY-MM-DD)")],
    symbol: Annotated[str, Query(description="Stock symbol")],
    store: Store,
) -> dict[str, Any]:
    target_date = parse_iso_date(date, field="date")
    normalized_symbol = symbol_master.to_db(symbol)
    not_found = "Snapshot row not found for requested date/symbol"

    try:
        frame = store.load(SnapshotKind.STOCK_DAILY, target_date)
    except SnapshotError as exc:
        _raise_http(exc, not_found)
    rows = to_records(frame[frame["symbol"] == normalized_symbol])
    if not rows:
        raise HTTPException(status_code=404, detail=not_found)

    return {
        "date": target_date.isoformat(),
        "symbol": normalized_symbol,
        "row": rows[0],
        "artifact": str(store.path(SnapshotKind.STOCK_DAILY)),
    }


@router.get(
    "/snapshot/universe",
    responses=SNAPSHOT_RESPONSES,
)
def get_universe_snapshot(
    date: Annotated[str, Query(description="Snapshot date (YYYY-MM-DD)")],
    store: Store,
    universe: Annotated[str, Query(description="Universe identifier")] = "NIFTY50",
) -> dict[str, Any]:
    target_date = parse_iso_date(date, field="date")
    universe_id = universe.strip().upper()
    if universe_id != "NIFTY50":
        detail = "Phase-1 currently supports universe=NIFTY50 only"
        raise HTTPException(status_code=400, detail=detail)

    try:
        frame = store.load(SnapshotKind.NIFTY50_DAILY, target_date)
    except SnapshotError as exc:
        _raise_http(exc, "No universe snapshot rows found for requested date")
    rows = to_records(frame)

    return {
        "date": target_date.isoformat(),
        "universe": universe_id,
        "count": len(rows),
        "rows": rows,
        "artifact": str(store.path(SnapshotKind.NIFTY50_DAILY)),
    }


@router.get("/snapshot/status")
def get_snapshot_status(db: DBSession, store: Store) -> dict[str, Any]:
    status = {
        "phase1_root_exists": DATA_ROOT.exists(),
        "curated": store.curated_status(
            [
                "equity_ohlcv.parquet",
                "equity_ohlcv_adj.parquet",
                "nifty50_weights_monthly.parquet",
                "nifty50_membership_daily.parquet",
                SnapshotKind.STOCK_DAILY.filename,
                SnapshotKind.NIFTY50_DAILY.filename,
            ]
        ),
        "metadata_files": store.metadata_status(
            [
                "source_manifest.json",
                "data_contract.json",
                "checksums.json",
                "run_log.jsonl",
            ]
        ),
        "latest_run_log": store.latest_run_log(),
        "latest_db_run": _serialize_latest_db_run(db),
    }

    return status
