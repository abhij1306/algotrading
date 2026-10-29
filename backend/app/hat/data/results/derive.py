"""The derive step: `hat_financial_growth` rebuilt from `hat_financial_result` (plan A3), and the
point-in-time read `results_as_of`.

One growth row per result row: the growth of the row's (instrument, period_end) as known when the
row became public (`growth.growth_as_of`), and whether the row itself is unit-suspect as known
then (`growth.suspect_row`). A suspect row is left out of every comparison made at or after the
time it became public. Both apply the basis rule (`scope.py`, consolidated-first by default) to
what is stored, so rows stored under another rule never reach a comparison. The table is derived:
each run replaces it whole, in one transaction that locks both tables first.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ...models import HatFinancialGrowth, HatFinancialResult
from ...time import as_utc
from .growth import INSURANCE, ResultRow, growth_as_of, known_at, suspect_row
from .scope import BASIS_CONSOLIDATED_FIRST


def result_row(row: HatFinancialResult) -> ResultRow:
    return ResultRow(
        str(row.id),
        row.period_end,
        row.basis,
        row.source,
        row.format,
        row.version,
        as_utc(row.public_at),
        row.revenue,
        row.eps_basic,
    )


def growth_rows(
    instrument_id: uuid.UUID, rows: Sequence[ResultRow], rule: str = BASIS_CONSOLIDATED_FIRST
) -> list[HatFinancialGrowth]:
    """The growth rows of one instrument's result rows under the basis rule `rule` (pure but for
    the model objects)."""
    ordered = sorted((r for r in rows if r.format != INSURANCE), key=lambda r: r.public_at)
    suspect: set[str] = set()
    out = []
    for row in ordered:
        flagged = suspect_row(row, ordered, suspect, rule)
        if flagged:
            suspect.add(row.id)
        found = growth_as_of(ordered, row.period_end, row.public_at, suspect, rule)
        out.append(
            HatFinancialGrowth(
                result_id=uuid.UUID(row.id),
                instrument_id=instrument_id,
                period_end=row.period_end,
                as_of=row.public_at,
                basis=found.basis,
                current_id=uuid.UUID(found.current_id) if found.current_id else None,
                prior_id=uuid.UUID(found.prior_id) if found.prior_id else None,
                sales_yoy=found.sales_yoy,
                eps_yoy=found.eps_yoy,
                turnaround=found.turnaround,
                unit_suspect=flagged,
                reason=found.reason,
            )
        )
    return out


@dataclass
class DeriveReport:
    instruments: int = 0
    rows: int = 0
    unit_suspect: int = 0
    defined_sales: int = 0
    defined_eps: int = 0


def derive(rule: str = BASIS_CONSOLIDATED_FIRST) -> DeriveReport:  # pragma: no cover - the database
    from app.database import db_session_scope

    report = DeriveReport()
    with db_session_scope(writes_data=True) as session:
        # one snapshot: no parse commits rows between this read and the replacement (SHARE
        # blocks inserts until commit), and no second derive replaces the table alongside
        on_live = {"mapper": HatFinancialResult}
        session.execute(
            text(f"LOCK TABLE {HatFinancialGrowth.__tablename__} IN EXCLUSIVE MODE"),
            bind_arguments=on_live,
        )
        session.execute(
            text(f"LOCK TABLE {HatFinancialResult.__tablename__} IN SHARE MODE"),
            bind_arguments=on_live,
        )
        by_instrument: dict[uuid.UUID, list[ResultRow]] = defaultdict(list)
        for row in session.scalars(select(HatFinancialResult)):
            by_instrument[row.instrument_id].append(result_row(row))
        session.execute(delete(HatFinancialGrowth))
        for instrument_id, rows in by_instrument.items():
            made = growth_rows(instrument_id, rows, rule)
            session.add_all(made)
            report.instruments += 1
            report.rows += len(made)
            report.unit_suspect += sum(1 for g in made if g.unit_suspect)
            report.defined_sales += sum(1 for g in made if g.sales_yoy is not None)
            report.defined_eps += sum(1 for g in made if g.eps_yoy is not None)
            session.flush()
    return report


def results_as_of(
    session: Session,
    instrument_id: uuid.UUID,
    ts: datetime,
    rule: str = BASIS_CONSOLIDATED_FIRST,
    *,
    announced_only: bool = False,
) -> list[HatFinancialResult]:
    """The instrument's results known at `ts`: only rows with `public_at <= ts`, the latest
    version of each (period_end, basis), a secondary-source row only where no NSE row of that
    key is public by `ts`, insurers' metadata rows left out, a standalone row only where the
    basis rule `rule` keeps it (`growth.known_at`); by period end, then basis. With
    `announced_only` the rows whose `public_at` is estimated are left out first, so a later
    estimated version never hides an announced one."""
    query = select(HatFinancialResult).where(
        HatFinancialResult.instrument_id == instrument_id,
        HatFinancialResult.public_at <= ts,
    )
    if announced_only:
        query = query.where(HatFinancialResult.public_at_estimated.is_(False))
    rows = list(session.scalars(query))
    by_id = {str(r.id): r for r in rows}
    known = known_at((result_row(r) for r in rows), ts, rule)
    return [by_id[known[key].id] for key in sorted(known)]


def growth_by_result(
    session: Session, result_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, HatFinancialGrowth]:
    """The growth row of each of `result_ids` that has one (the growth of the row's quarter as
    known when the row became public)."""
    if not result_ids:
        return {}
    rows = session.scalars(
        select(HatFinancialGrowth).where(HatFinancialGrowth.result_id.in_(list(result_ids)))
    )
    return {row.result_id: row for row in rows}
