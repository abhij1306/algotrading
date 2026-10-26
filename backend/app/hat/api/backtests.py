"""The backtest routes under `/api/hat/backtests`: the published runs, read from their folders
under `HAT_DATA_ROOT/backtests/runs/` (`backtest.folder`, docs/backtests/result-standard.md);
and the one write, starting a research run or a small sweep as a background job
(`backtest.jobs`: the backtest command line in a child process).

- `GET /api/hat/backtests/runs` (optional `strategy`, `sweep_id`, `split`): the runs, newest
  first, each its `run.json` without the breakdowns and config (`BacktestRunItem`).
- `GET /api/hat/backtests/runs/{run_id}`: the run's whole `run.json`.
- `GET /api/hat/backtests/runs/{run_id}/files/{name}`: one of its CSV files (`trades`, `equity`,
  `refusals`, `signals`, `stocks`) as `text/csv`.
- `GET /api/hat/backtests/runs/{run_id}/audits/{name}`: its run audit (`audit`) or the data audit
  it ran on (`data_audit`), as JSON. A list item carries the run audit's status and digest.
- `GET /api/hat/backtests/options`: the strategy versions a job can run, each with its exit stack,
  its playbooks' stops and its owner's universes (`BacktestOptions`).
- `POST /api/hat/backtests` (behind `guard.frontend_write`): start a job (`BacktestStart`): one
  variant is a plain research run, two to `jobs.MAX_VARIANTS` a sweep. Never a promotion run.
  202 with the job; 409 while another job runs; 422 with a fixed code for a request the command
  line would refuse before loading the market (an unknown version or universe, a bad period,
  label, override path, sleeve weight or slot order).
- `GET /api/hat/backtests/jobs`: the jobs started since the server started, newest first, each
  with the variants finished so far (`BacktestJob`).

An unknown run or file (or a run id that is not one) is a 404; a failed read answers a fixed
message.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...database import get_db
from ..backtest import folder
from ..backtest.jobs import JOB_BUSY, MAX_VARIANTS, Job, Jobs, JobVariant, check_variants
from ..backtest.spec import BacktestError
from ..models import HatStrategy, HatStrategyVersion, HatUniverse
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
ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such run"},
    503: {"description": "The database could not be read"},
    500: {"description": "The read failed"},
}
FILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {"schema": {"type": "string"}}}, "description": "The CSV"},
    404: {"description": "No such run or file"},
    500: {"description": "The read failed"},
}
# The `run.json` blocks a run's list item carries (`docs/backtests/result-standard.md`).
LIST_BLOCKS = ("identity", "data", "metrics", "refusals", "binding")
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


class BacktestRunItem(BaseModel):
    """A run in the list: its `run.json` blocks as published (the schema's shapes)."""

    identity: dict[str, Any]
    data: dict[str, Any]
    metrics: dict[str, Any]
    refusals: dict[str, Any]
    binding: dict[str, Any]
    audit: dict[str, Any] | None = None  # the run audit's status and digest; None without one


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
    status: str | None  # the run's complete, failed or stopped_early, or refused
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


def _matches(
    doc: dict[str, Any], strategy: str | None, sweep_id: str | None, split: str | None
) -> bool:
    identity, data = doc.get("identity") or {}, doc.get("data") or {}
    return (
        (strategy is None or identity.get("strategy") == strategy)
        and (sweep_id is None or identity.get("sweep_id") == sweep_id)
        and (split is None or data.get("split") == split)
    )


def _audit_of(root: Path, doc: dict[str, Any]) -> dict[str, Any] | None:
    found = folder.read_doc(root, str((doc.get("identity") or {}).get("run_id")), "audit")
    return None if found is None else {"status": found.get("status"), "digest": found.get("digest")}


def _runs(
    root: Path, strategy: str | None, sweep_id: str | None, split: str | None
) -> list[BacktestRunItem]:
    return [
        BacktestRunItem(**{k: doc.get(k) or {} for k in LIST_BLOCKS}, audit=_audit_of(root, doc))
        for doc in folder.list_runs(root)
        if _matches(doc, strategy, sweep_id, split)
    ]


def _audit(root: Path, run_id: str, name: str) -> dict[str, Any]:
    doc = folder.read_doc(root, run_id, name)
    if doc is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return doc


def _run(root: Path, run_id: str) -> dict[str, Any]:
    doc = folder.read_run(root, run_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return doc


def _file(root: Path, run_id: str, name: str) -> Response:
    text = folder.read_csv(root, run_id, name)
    if text is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return Response(content=text, media_type="text/csv; charset=utf-8")


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


@router.get("/runs", responses=ERRORS)
def list_runs(
    root: Root, strategy: str | None = None, sweep_id: str | None = None, split: str | None = None
) -> list[BacktestRunItem]:
    """The published runs, newest first, filtered by strategy label, sweep id and split."""
    return _read("list", lambda: _runs(root, strategy, sweep_id, split))


@router.get("/runs/{run_id}", responses=ERRORS)
def get_run(run_id: str, root: Root) -> dict[str, Any]:
    """One run's whole `run.json`."""
    return _read("run", lambda: _run(root, run_id))


@router.get("/runs/{run_id}/files/{name}", response_class=Response, responses=FILE_RESPONSES)
def get_file(run_id: str, name: str, root: Root) -> Response:
    """One of the run's CSV files: trades, equity, refusals, signals or stocks."""
    return _read("file", lambda: _file(root, run_id, name))


@router.get("/runs/{run_id}/audits/{name}", responses=ERRORS)
def get_audit(run_id: str, name: str, root: Root) -> dict[str, Any]:
    """The run's run audit (`audit`) or the data audit it ran on (`data_audit`)."""
    return _read("audit", lambda: _audit(root, run_id, name))


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
