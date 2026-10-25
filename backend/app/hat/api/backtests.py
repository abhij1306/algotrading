"""The backtest routes under `/api/hat/backtests`: the recorded runs, one run with its report
data, and its trades (`backtest.report_data`), read from Postgres and the run's files under
`HAT_DATA_ROOT`; and the one write, starting a research run or a small sweep as a background job
(`backtest.jobs`: the backtest command line in a child process).

- `GET /api/hat/backtests` (`limit`, optional `family_key`, `sweep_id`): the runs, newest first,
  each with its headline metrics (`BacktestRunSummary`).
- `GET /api/hat/backtests/{run_id}`: the run's summary, its stored metrics (the document
  `report.compute_metrics` and `runs.complete_run` write) and its report data without the
  trades (`BacktestRunDetail`; `report` is null for a run without one: older, failed or running).
- `GET /api/hat/backtests/{run_id}/trades`: the run's trades in exit order, each with its symbol
  (`BacktestTrade`); an empty list for a run without report data.
- `GET /api/hat/backtests/options`: the strategy versions a job can run, each with its exit stack,
  its playbooks' stops and its owner's universes (`BacktestOptions`).
- `POST /api/hat/backtests` (behind `guard.frontend_write`): start a job (`BacktestStart`): one
  variant is a plain research run, two to `jobs.MAX_VARIANTS` a sweep. Never a promotion run.
  202 with the job; 409 while another job runs; 422 with a fixed code for a request the command
  line would refuse before loading the market (an unknown version or universe, a bad period,
  label, override path, sleeve weight or slot order).
- `GET /api/hat/backtests/jobs`: the jobs started since the server started, newest first, each
  with the variants finished so far (`BacktestJob`).

Money and R are exact decimal strings, return and drawdown ratios floats, as `report_data` stores
them. An unknown run is a 404; a failed read answers a fixed message.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...database import get_db
from ..backtest.jobs import JOB_BUSY, MAX_VARIANTS, Job, Jobs, JobVariant, check_variants
from ..backtest.report_data import read_report, report_path
from ..backtest.spec import BacktestError
from ..models import HatBacktestRun, HatInstrument, HatStrategy, HatStrategyVersion, HatUniverse
from ..money import exact
from ..settings import operator_settings
from .guard import frontend_write

__all__ = ["data_root", "jobs", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hat/backtests", tags=["HAT backtests"])

DB_ERROR = "The HAT database could not be read"
READ_ERROR = "The backtest could not be read"
NOT_FOUND = "No such backtest run"
START_ERROR = "The backtest job could not be started"
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


START_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "Another backtest job is running"},
    422: {"description": "The request was refused: a fixed code"},
    503: {"description": "The database could not be read"},
    500: {"description": "The job could not be started"},
}
JOBS = Jobs()


def data_root() -> Path:
    """The HAT data root the runs' files live under (a dependency, so a check can point it)."""
    return operator_settings().data_root


def jobs() -> Jobs:
    """The app's backtest jobs (a dependency, so a check can swap them)."""
    return JOBS


Db = Annotated[Session, Depends(get_db)]
Root = Annotated[Path, Depends(data_root)]
JobsDep = Annotated[Jobs, Depends(jobs)]


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


class BacktestUniverse(BaseModel):
    id: uuid.UUID
    name: str


class BacktestPlaybookStop(BaseModel):
    name: str  # the playbook's name
    stop: dict[str, Any]  # its stop rule as the config holds it (`rule` and its parameters)


class BacktestVersionOption(BaseModel):
    id: uuid.UUID
    strategy: str
    version: int
    status: str
    exits: list[dict[str, Any]]  # the exit stack as the config holds it, in order
    playbooks: list[BacktestPlaybookStop]
    universes: list[BacktestUniverse]  # the strategy owner's universes


class BacktestOptions(BaseModel):
    versions: list[BacktestVersionOption]
    max_variants: int


class BacktestSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # a config path (`overrides`), e.g. `exits[2].at_r`
    value: JsonValue


class BacktestVariantStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = "run"  # a sweep's variant name, unique in the sweep
    sets: list[BacktestSet] = Field(default_factory=list)
    risk_pct: str | None = None  # a decimal fraction, e.g. "0.005"
    max_positions: int | None = None
    sleeve_weight: str | None = None  # a decimal in (0, 1]
    slot_order: str | None = None  # time, score or random:K


class BacktestStart(BaseModel):
    """A research run (one variant) or a sweep (two or more); never a promotion run."""

    model_config = ConfigDict(extra="forbid")

    strategy_version_id: uuid.UUID
    universe_id: uuid.UUID
    start: date
    end: date
    variants: list[BacktestVariantStart] = Field(min_length=1, max_length=MAX_VARIANTS)


class BacktestJobOutcome(BaseModel):
    label: str | None  # the sweep variant's label; None for a plain run
    run_id: uuid.UUID | None  # None when the variant was refused before it was recorded
    status: str | None  # completed, failed or refused
    code: str | None  # the refusal or failure code


class BacktestJob(BaseModel):
    id: uuid.UUID
    status: str  # running, completed, failed or refused
    code: str | None  # why a failed or refused job ended
    strategy_version_id: uuid.UUID
    universe: str
    start: date
    end: date
    labels: list[str]
    sweep: bool
    total: int  # the variants it runs
    outcomes: list[BacktestJobOutcome]  # the variants finished so far, in order
    started_at: datetime
    finished_at: datetime | None


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


def _options(session: Session) -> BacktestOptions:
    rows = session.execute(
        select(HatStrategyVersion, HatStrategy)
        .join(HatStrategy, HatStrategy.id == HatStrategyVersion.strategy_id)
        .order_by(HatStrategy.name, HatStrategyVersion.version)
    ).all()
    universes: dict[uuid.UUID, list[BacktestUniverse]] = {}
    for u in session.scalars(select(HatUniverse).order_by(HatUniverse.name)):
        universes.setdefault(u.user_id, []).append(BacktestUniverse(id=u.id, name=u.name))
    versions = []
    for version, strategy in rows:
        config = dict(version.config or {})
        versions.append(
            BacktestVersionOption(
                id=version.id,
                strategy=strategy.name,
                version=version.version,
                status=version.status,
                exits=list(config.get("exits") or []),
                playbooks=[
                    BacktestPlaybookStop(name=p.get("name", ""), stop=p.get("stop") or {})
                    for p in config.get("playbooks") or []
                ],
                universes=universes.get(strategy.user_id, []),
            )
        )
    return BacktestOptions(versions=versions, max_variants=MAX_VARIANTS)


def _job(job: Job) -> BacktestJob:
    return BacktestJob(
        id=job.id,
        status=job.status,
        code=job.code,
        strategy_version_id=job.strategy_version_id,
        universe=job.universe,
        start=job.start,
        end=job.end,
        labels=list(job.labels),
        sweep=job.sweep,
        total=len(job.labels),
        outcomes=[BacktestJobOutcome(**o) for o in job.outcomes],
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _decimal_field(text: str | None, code: str) -> Decimal | None:
    if text is None:
        return None
    number = exact(text)
    if number is None:
        raise BacktestError(code)
    return number


def _variant(item: BacktestVariantStart) -> JobVariant:
    return JobVariant(
        label=item.label,
        sets=tuple((s.path, s.value) for s in item.sets),
        risk_pct=_decimal_field(item.risk_pct, "backtest_override_invalid"),
        max_positions=item.max_positions,
        sleeve_weight=_decimal_field(item.sleeve_weight, "backtest_bad_sleeve_weight"),
        slot_order=item.slot_order,
    )


def _check_target(session: Session, body: BacktestStart) -> None:
    """Refuse an unknown version, a universe its strategy's owner does not hold and an empty
    period, as the command line would before loading the market."""
    version = session.get(HatStrategyVersion, body.strategy_version_id)
    strategy = None if version is None else session.get(HatStrategy, version.strategy_id)
    if strategy is None:
        raise BacktestError("backtest_unknown_version")
    universe = session.get(HatUniverse, body.universe_id)
    if universe is None or universe.user_id != strategy.user_id:
        raise BacktestError("backtest_no_universe")
    if body.start >= body.end:
        raise BacktestError("backtest_bad_period")


def _start(session: Session, root: Path, registry: Jobs, body: BacktestStart) -> BacktestJob:
    try:
        variants = [_variant(item) for item in body.variants]
        check_variants(variants)
        _check_target(session, body)
        job = registry.start(
            root, body.strategy_version_id, str(body.universe_id), body.start, body.end, variants
        )
    except BacktestError as exc:
        status = 409 if exc.code == JOB_BUSY else 422
        raise HTTPException(status_code=status, detail=exc.code) from None
    except SQLAlchemyError:
        logger.exception("HAT backtests start: database read failed")
        raise HTTPException(status_code=503, detail=DB_ERROR) from None
    except Exception:
        logger.exception("HAT backtests start: the job could not be started")
        raise HTTPException(status_code=500, detail=START_ERROR) from None
    return _job(job)


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


@router.get("/options", responses=ERRORS)
def get_options(db: Db) -> BacktestOptions:
    """The strategy versions a job can run, with their exits, stops and universes."""
    return _read("options", lambda: _options(db))


@router.get("/jobs")
def list_jobs(registry: JobsDep) -> list[BacktestJob]:
    """The jobs started since the server started, newest first."""
    return [_job(job) for job in registry.list()]


@router.post("", status_code=202, responses=START_ERRORS, dependencies=[Depends(frontend_write)])
def start_job(body: BacktestStart, db: Db, root: Root, registry: JobsDep) -> BacktestJob:
    """Start a research run or a sweep as a background job (`backtest.jobs`)."""
    return _start(db, root, registry, body)


@router.get("/{run_id}", responses=ERRORS)
def get_run(run_id: uuid.UUID, db: Db, root: Root) -> BacktestRunDetail:
    """One run: its summary, stored metrics and report data (without the trades)."""
    return _read("run", lambda: _detail(db, root, run_id))


@router.get("/{run_id}/trades", responses=ERRORS)
def get_trades(run_id: uuid.UUID, db: Db, root: Root) -> list[BacktestTrade]:
    """One run's trades in exit order, with their symbols."""
    return _read("trades", lambda: _trades(db, root, run_id))
