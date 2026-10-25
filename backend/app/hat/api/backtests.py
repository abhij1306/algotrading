"""The read-only backtest routes under `/api/hat/backtests`: the recorded runs, one run with its
report data, and its trades (`backtest.report_data`). Every route is a GET that reads Postgres
and the run's files under `HAT_DATA_ROOT` and changes nothing; a backtest is started from the
command line (`python -m app.hat.backtest`).

- `GET /api/hat/backtests` (`limit`, optional `family_key`, `sweep_id`): the runs, newest first,
  each with its headline metrics (`BacktestRunSummary`).
- `GET /api/hat/backtests/{run_id}`: the run's summary, its stored metrics (the document
  `report.compute_metrics` and `runs.complete_run` write) and its report data without the
  trades (`BacktestRunDetail`; `report` is null for a run without one: older, failed or running).
- `GET /api/hat/backtests/{run_id}/trades`: the run's trades in exit order, each with its symbol
  (`BacktestTrade`); an empty list for a run without report data.

Money and R are exact decimal strings, return and drawdown ratios floats, as `report_data` stores
them. An unknown run is a 404; a failed read answers a fixed message.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...database import get_db
from ..backtest.report_data import read_report, report_path
from ..models import HatBacktestRun, HatInstrument, HatStrategy, HatStrategyVersion
from ..settings import operator_settings

__all__ = ["data_root", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hat/backtests", tags=["HAT backtests"])

DB_ERROR = "The HAT database could not be read"
READ_ERROR = "The backtest could not be read"
NOT_FOUND = "No such backtest run"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such run"},
    503: {"description": "The database could not be read"},
    500: {"description": "The read failed"},
}
# The headline metrics a run's summary carries, as stored (`report.compute_metrics`).
HEADLINE = (
    "trades",
    "signals",
    "sum_r",
    "expectancy_r",
    "win_rate",
    "avg_win_r",
    "avg_loss_r",
    "avg_mfe_r",
    "avg_mae_r",
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe_annual",
    "exposure",
    "net_pnl",
    "fees",
    "sleeve_weight",
    "slot_order",
    "regime_mult",
    "slippage_bps",
    "for_promotion",
    "config_digest",
    "completed_at",
    "error",
)

T = TypeVar("T")


def data_root() -> Path:
    """The HAT data root the runs' files live under (a dependency, so a check can point it)."""
    return operator_settings().data_root


Db = Annotated[Session, Depends(get_db)]
Root = Annotated[Path, Depends(data_root)]


def _read(what: str, read: Callable[[], T]) -> T:
    """Run a read; an unknown run is a 404, any other failure a fixed message."""
    try:
        return read()
    except HTTPException:
        raise
    except SQLAlchemyError:
        logger.exception("HAT backtests %s: database read failed", what)
        raise HTTPException(status_code=503, detail=DB_ERROR) from None
    except Exception:
        logger.exception("HAT backtests %s: read failed", what)
        raise HTTPException(status_code=500, detail=READ_ERROR) from None


# --- response models ---------------------------------------------------------------------------


class BacktestVariant(BaseModel):
    label: str
    sweep_id: str


class BacktestRunSummary(BaseModel):
    id: uuid.UUID
    status: str  # running, completed or failed
    family_key: str
    strategy: str | None  # the strategy's name
    strategy_version_id: uuid.UUID
    strategy_version: int | None
    risk_policy_version_id: uuid.UUID
    period_start: date
    period_end: date
    variant: BacktestVariant | None  # a sweep's run: its label and the sweep's id
    overrides: dict[str, Any] | None  # `conventions.overrides`: `set`, `risk_pct`, ...
    dsr: str | None  # the deflated Sharpe probability at completion
    pbo: str | None  # the family's PBO at completion
    has_report: bool
    # the stored metrics named in `HEADLINE` (absent ones left out): numbers, or exact strings
    headline: dict[str, Any]


class BacktestGroupStats(BaseModel):
    key: str  # the exit reason, hold bucket (`0-5`, `6-20`, `21-60`, `61+`) or regime
    trades: int
    sum_r: str
    mean_r: str
    win_rate: float


class BacktestEquityPoint(BaseModel):
    date: date
    equity: str  # the account's mark at the session's close
    drawdown: float  # from the running peak, <= 0
    benchmark: float | None  # NIFTY 500 rebased to 1 at the run's first session


class BacktestYear(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    year: int
    # the calendar year's return, sent as `return`; None after a mark at or below 0
    return_: float | None = Field(alias="return")
    benchmark: float | None  # NIFTY 500's, None without the index


class BacktestRPoint(BaseModel):
    date: date  # the trade's exit date
    r: str  # cumulative R in exit order


class BacktestBenchmark(BaseModel):
    name: str
    total_return: float | None
    cagr: float | None
    max_drawdown: float


class BacktestFunnel(BaseModel):
    watchlisted: int  # watchlist rows published
    skipped: int  # triggers that never became a signal
    skips: dict[str, int]
    signals: int
    rejected: int  # signals the risk authority sized at 0
    rejections: dict[str, int]
    entered: int
    trades: int  # closed trades


class BacktestReport(BaseModel):
    version: int
    benchmark: BacktestBenchmark | None
    regime_rule: str
    equity: list[BacktestEquityPoint]
    yearly: list[BacktestYear]
    r_curve: list[BacktestRPoint]
    exit_reasons: list[BacktestGroupStats]
    hold_time: list[BacktestGroupStats]
    regime: list[BacktestGroupStats]
    funnel: BacktestFunnel


class BacktestRunDetail(BaseModel):
    run: BacktestRunSummary
    metrics: dict[str, Any]  # the run's stored metrics
    report: BacktestReport | None


class BacktestTrade(BaseModel):
    entry_date: date
    exit_date: date
    playbook: str
    instrument_id: str
    symbol: str | None
    qty: int
    entry_price: str
    initial_stop: str
    exit_reason: str
    realized_pnl: str
    fees: str
    r_multiple: str
    mfe_r: str
    mae_r: str
    holding_days: int
    score: str | None
    regime: str  # `up`, `not_up` or `unknown` (`BacktestReport.regime_rule`)
    auction_risk: str | None


# --- reads -------------------------------------------------------------------------------------


def _summary(
    run: HatBacktestRun,
    version: HatStrategyVersion | None,
    strategy: HatStrategy | None,
    root: Path,
) -> BacktestRunSummary:
    metrics = dict(run.metrics or {})
    conventions = metrics.get("conventions") or {}
    variant = metrics.get("variant")
    return BacktestRunSummary(
        id=run.id,
        status=run.status,
        family_key=run.family_key,
        strategy=None if strategy is None else strategy.name,
        strategy_version_id=run.strategy_version_id,
        strategy_version=None if version is None else version.version,
        risk_policy_version_id=run.risk_policy_version_id,
        period_start=run.period_start,
        period_end=run.period_end,
        variant=BacktestVariant(**variant) if isinstance(variant, dict) else None,
        overrides=conventions.get("overrides"),
        dsr=(metrics.get("dsr") or {}).get("prob"),
        pbo=(metrics.get("pbo") or {}).get("prob"),
        has_report=report_path(root, run.id).is_file(),
        headline={k: metrics[k] for k in HEADLINE if k in metrics},
    )


def _runs(
    session: Session, root: Path, limit: int, family_key: str | None, sweep_id: str | None
) -> list[BacktestRunSummary]:
    query = (
        select(HatBacktestRun, HatStrategyVersion, HatStrategy)
        .outerjoin(HatStrategyVersion, HatStrategyVersion.id == HatBacktestRun.strategy_version_id)
        .outerjoin(HatStrategy, HatStrategy.id == HatStrategyVersion.strategy_id)
        .order_by(HatBacktestRun.id.desc())
        .limit(limit)
    )
    if family_key is not None:
        query = query.where(HatBacktestRun.family_key == family_key)
    if sweep_id is not None:
        query = query.where(HatBacktestRun.metrics["variant"]["sweep_id"].as_string() == sweep_id)
    return [_summary(run, v, s, root) for run, v, s in session.execute(query).all()]


def _run(session: Session, run_id: uuid.UUID) -> HatBacktestRun:
    run = session.get(HatBacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return run


def _detail(session: Session, root: Path, run_id: uuid.UUID) -> BacktestRunDetail:
    run = _run(session, run_id)
    version = session.get(HatStrategyVersion, run.strategy_version_id)
    strategy = None if version is None else session.get(HatStrategy, version.strategy_id)
    found = read_report(root, run.id)
    report = None
    if found is not None:
        report = BacktestReport.model_validate({k: v for k, v in found.items() if k != "trades"})
    return BacktestRunDetail(
        run=_summary(run, version, strategy, root), metrics=dict(run.metrics or {}), report=report
    )


def _trades(session: Session, root: Path, run_id: uuid.UUID) -> list[BacktestTrade]:
    run = _run(session, run_id)
    found = read_report(root, run.id)
    trades = [] if found is None else list(found.get("trades") or [])
    ids = {uuid.UUID(t["instrument_id"]) for t in trades}
    symbols = (
        dict(
            session.execute(
                select(HatInstrument.id, HatInstrument.symbol).where(HatInstrument.id.in_(ids))
            ).all()
        )
        if ids
        else {}
    )
    return [BacktestTrade(**t, symbol=symbols.get(uuid.UUID(t["instrument_id"]))) for t in trades]


# --- routes ------------------------------------------------------------------------------------


@router.get("", responses=ERRORS)
def list_runs(
    db: Db,
    root: Root,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    family_key: str | None = None,
    sweep_id: str | None = None,
) -> list[BacktestRunSummary]:
    """The recorded runs, newest first."""
    return _read("list", lambda: _runs(db, root, limit, family_key, sweep_id))


@router.get("/{run_id}", responses=ERRORS)
def get_run(run_id: uuid.UUID, db: Db, root: Root) -> BacktestRunDetail:
    """One run: its summary, stored metrics and report data (without the trades)."""
    return _read("run", lambda: _detail(db, root, run_id))


@router.get("/{run_id}/trades", responses=ERRORS)
def get_trades(run_id: uuid.UUID, db: Db, root: Root) -> list[BacktestTrade]:
    """One run's trades in exit order, with their symbols."""
    return _read("trades", lambda: _trades(db, root, run_id))
