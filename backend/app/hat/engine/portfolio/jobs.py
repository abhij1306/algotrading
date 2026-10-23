"""The two nightly jobs of the portfolio layer: `regime` then `allocate` (05 §3, §5; 10 §3).

Each body runs inside `run_job` (which records the run once per trade date) and in the caller's
transaction; the caller commits. A body fails with a stable `JobError` code when an input is
missing, so the cycle stops rather than allocate on a guess.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.hat.backtest.runs import backtest_evidence
from app.hat.data.store import DATASET_BREADTH, DATASET_INDEX, DATASET_MARKET_BREADTH, DataStore
from app.hat.errors import HatError
from app.hat.ops.jobs import JobError, JobResult

from . import regime
from .service import (
    BacktestEvidence,
    PortfolioError,
    ensure_regime_model,
    market_id_of,
    record_regime,
    run_allocation,
)

REGIME_JOB = "regime"
ALLOCATE_JOB = "allocate"
INDEX_NAME = regime.INDEX_NAME
VIX_NAME = regime.VIX_NAME
HISTORY_DAYS = regime.HISTORY_DAYS


def read_series(store: DataStore, day: date) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    """The NIFTY 500 and India VIX closes of the last `HISTORY_DAYS` index days up to `day`."""
    days = [d for d in store.days(DATASET_INDEX) if d <= day][-HISTORY_DAYS:]
    closes: dict[date, Decimal] = {}
    vix: dict[date, Decimal] = {}
    for d in days:
        for row in store.read_index(d):
            if row.index_name == INDEX_NAME:
                closes[d] = row.close
            elif row.index_name == VIX_NAME:
                vix[d] = row.close
    return closes, vix


def read_breadth(store: DataStore, day: date) -> Decimal | None:
    """The `breadth_pct` of `day` from the features job, None when it has not been written."""
    frame = store.read_partition(DATASET_BREADTH, day)
    if frame.height == 0:
        return None
    value = frame["breadth_pct"][0]
    return None if value is None else Decimal(str(value))


def read_ad_ratio(store: DataStore, day: date) -> Decimal | None:
    """The advance/decline ratio the ingest wrote for `day` (HAT-DATA-044); None when absent.
    It is recorded with the regime inputs and never decides the regime."""
    frame = store.read_partition(DATASET_MARKET_BREADTH, day)
    if frame.height == 0:
        return None
    value = frame["ad_ratio"][0]
    if value is None:
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def regime_job(session: Session, store: DataStore, trade_date: date) -> JobResult:
    """`regime`: classify `trade_date` and store its `hat_regime_state` row."""
    closes, vix = read_series(store, trade_date)
    try:
        inputs = regime.compute_inputs(
            trade_date,
            closes,
            vix,
            read_breadth(store, trade_date),
            read_ad_ratio(store, trade_date),
        )
        market_id = market_id_of(session)
    except (regime.RegimeError, PortfolioError) as exc:
        raise JobError(exc.code) from exc
    model = ensure_regime_model(session, market_id)
    record_regime(session, trade_date, inputs, market_id=market_id, model=model)
    return JobResult(calls=0, rows=1)


def allocate_job(
    session: Session,
    trade_date: date,
    *,
    user_id: uuid.UUID,
    total_equity: Callable[[], Decimal],
    backtest: BacktestEvidence = backtest_evidence,
) -> JobResult:
    """`allocate`: one `hat_allocation_daily` row per sleeve for `trade_date`. `total_equity` is
    the account equity source (broker funds at session start, [OWNER-PENDING] HAT-PORT-052).
    The lifecycle reads each sleeve's backtest evidence from its recorded runs (HAT-BT-060)."""
    try:
        report = run_allocation(
            session,
            trade_date,
            user_id=user_id,
            total_equity=total_equity(),
            backtest=backtest,
        )
    except PortfolioError as exc:
        raise JobError(exc.code) from exc
    except HatError as exc:  # an unreadable sleeve equity or an unusable total
        raise JobError("allocate_failed") from exc
    return JobResult(calls=0, rows=report.rows)
