"""Point-in-time growth from stored results (plan A3), the unit sanity check (A2 rule 5) and the
Q4 derivation (A2 rule 4). Pure: plain rows in, values out; `derive.py` and `store.py` feed it.

- What is known at `ts` (`known_at`): for each (period_end, basis), the latest version among rows
  public at or before `ts`; a secondary-source row only where no NSE row of that key is public
  by then; insurers' metadata rows never.
- Growth of a quarter (`growth`): against the same quarter a year earlier on one basis:
  consolidated when both quarters have it, else standalone when both have it, else undefined;
  never mixed. sales_yoy = revenue / prior revenue - 1 (prior revenue > 0); eps_yoy = eps /
  prior eps - 1 when both are > 0; turnaround = prior eps <= 0 and eps > 0. A row flagged
  unit_suspect is treated as absent.
- unit_suspect: quarterly revenue more than 20x above or below the median of the same company's
  previous 4 quarters on the same basis (as known when the row became public).
"""

from __future__ import annotations

import calendar
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .xbrl import Figures

CONSOLIDATED = "consolidated"
STANDALONE = "standalone"
NSE = "nse"
INSURANCE = "insurance_skipped"
UNIT_SUSPECT_MULTIPLE = Decimal(20)
UNIT_LOOKBACK_QUARTERS = 4
UNIT_MIN_HISTORY = 2


@dataclass(frozen=True)
class ResultRow:
    """The fields of a `hat_financial_result` row growth reads."""

    id: str
    period_end: date
    basis: str
    source: str
    format: str
    version: int
    public_at: datetime
    revenue: Decimal | None
    eps_basic: Decimal | None


Key = tuple[date, str]  # (period_end, basis)


def known_at(rows: Iterable[ResultRow], ts: datetime) -> dict[Key, ResultRow]:
    """The row of each (period_end, basis) known at `ts` (rules above)."""
    nse: dict[Key, ResultRow] = {}
    secondary: dict[Key, ResultRow] = {}
    for row in rows:
        if row.public_at > ts or row.format == INSURANCE:
            continue
        key = (row.period_end, row.basis)
        bucket = nse if row.source == NSE else secondary
        held = bucket.get(key)
        if held is None or (row.version, row.public_at) > (held.version, held.public_at):
            bucket[key] = row
    return {**{k: v for k, v in secondary.items() if k not in nse}, **nse}


def quarter_ends_before(period_end: date, count: int) -> list[date]:
    """The `count` quarter ends before `period_end`, nearest first."""
    out = []
    year, month = period_end.year, period_end.month
    for _ in range(count):
        month -= 3
        if month <= 0:
            month += 12
            year -= 1
        out.append(date(year, month, calendar.monthrange(year, month)[1]))
    return out


def year_ago(period_end: date) -> date:
    """The same quarter end a year earlier."""
    year = period_end.year - 1
    return date(year, period_end.month, calendar.monthrange(year, period_end.month)[1])


def unit_suspect(revenue: Decimal | None, previous: Sequence[Decimal | None]) -> bool:
    """Whether `revenue` is more than 20x off the median of `previous` (the previous quarters'
    revenues, up to 4) in either direction; False without a positive revenue or with fewer than
    two positive previous revenues to compare with."""
    usable = [r for r in previous[:UNIT_LOOKBACK_QUARTERS] if r is not None and r > 0]
    if revenue is None or revenue <= 0 or len(usable) < UNIT_MIN_HISTORY:
        return False
    median = statistics.median(usable)
    return revenue > median * UNIT_SUSPECT_MULTIPLE or revenue * UNIT_SUSPECT_MULTIPLE < median


def suspect_row(row: ResultRow, rows: Iterable[ResultRow]) -> bool:
    """`unit_suspect` for `row` against its company's previous 4 quarters on its basis, as known
    when it became public."""
    known = known_at(rows, row.public_at)
    previous = [known.get((end, row.basis)) for end in quarter_ends_before(row.period_end, 4)]
    return unit_suspect(row.revenue, [p.revenue if p else None for p in previous])


@dataclass(frozen=True)
class Growth:
    basis: str | None
    current_id: str | None
    prior_id: str | None
    sales_yoy: Decimal | None
    eps_yoy: Decimal | None
    turnaround: bool | None
    reason: str | None  # why a value is undefined; None when all three are defined


def growth(known: Mapping[Key, ResultRow], period_end: date, suspect: set[str]) -> Growth:
    """The growth of `period_end` from `known` (`known_at`), on the basis rule above; rows whose
    id is in `suspect` are treated as absent."""
    prior_end = year_ago(period_end)

    def usable(key: Key) -> ResultRow | None:
        row = known.get(key)
        return row if row is not None and row.id not in suspect else None

    for basis in (CONSOLIDATED, STANDALONE):
        current, prior = usable((period_end, basis)), usable((prior_end, basis))
        if current is not None and prior is not None:
            return compare(basis, current, prior)
    return Growth(None, None, None, None, None, None, "no_common_basis")


def compare(basis: str, current: ResultRow, prior: ResultRow) -> Growth:
    reasons = []
    sales = None
    if current.revenue is None or prior.revenue is None:
        reasons.append("revenue_missing")
    elif prior.revenue <= 0:
        reasons.append("prior_revenue_not_positive")
    else:
        sales = current.revenue / prior.revenue - 1
    eps = None
    turnaround = None
    if current.eps_basic is None or prior.eps_basic is None:
        reasons.append("eps_missing")
    else:
        turnaround = prior.eps_basic <= 0 < current.eps_basic
        if current.eps_basic > 0 and prior.eps_basic > 0:
            eps = current.eps_basic / prior.eps_basic - 1
        else:
            reasons.append("eps_not_positive")
    return Growth(basis, current.id, prior.id, sales, eps, turnaround, ";".join(reasons) or None)


def growth_as_of(
    rows: Sequence[ResultRow], period_end: date, ts: datetime, suspect: set[str]
) -> Growth:
    """The growth of one company's `period_end` as known at `ts`."""
    return growth(known_at(rows, ts), period_end, suspect)


def derive_q4(annual: Figures, nine_month: Figures) -> Figures:
    """Q4 = the year less the nine months to December, field by field (same basis)."""
    return annual.minus(nine_month)
