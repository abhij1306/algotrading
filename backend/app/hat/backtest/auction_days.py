"""The sessions whose official close (the closing auction's) can sit far from the price live acts on
before the 15:15 cut-off (owner 2026-10-25): the backtest decides its close rules on the daily
close (`close_rules_decided_on_official_close`), and on these days that stand-in is worst, so the
run flags the exits it priced at such a close (`TradeRow.auction_risk`, `auction_risk_exits`).

Both are derived by rule from the sessions the store holds (no dated list):

- **F&O expiry** (`fo_expiry`): the monthly index and stock derivatives expiry, the last
  Thursday of the month for contracts expiring through 2025-08-31 and the last Tuesday from
  2025-09-01 (SEBI circular of 2025-05-26, NSE circular of 2025-06-17), moved to the previous
  trading day when that day is a holiday.
- **Index rebalance** (`index_rebalance`): the NIFTY indices' semi-annual reconstitution takes
  effect on the last trading day of March and September (NSE Indices, "Methodology Document for
  Equity Indices" and its index reconstitution calendar), so index funds trade the change at the
  close of the session before it.

A day is tagged only when the sessions known reach past its month's rule date, so a period that
ends mid-month never tags an earlier day by mistake.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

__all__ = [
    "FO_EXPIRY",
    "INDEX_REBALANCE",
    "TUESDAY_EXPIRY_FROM",
    "auction_risk_days",
    "index_rebalance_closes",
    "monthly_expiries",
]

FO_EXPIRY = "fo_expiry"
INDEX_REBALANCE = "index_rebalance"
TUESDAY_EXPIRY_FROM = date(2025, 9, 1)
_THURSDAY, _TUESDAY = 3, 1
_REBALANCE_MONTHS = (3, 9)


def _month_end(year: int, month: int) -> date:
    first_next = date(year + month // 12, month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    end = _month_end(year, month)
    return end - timedelta(days=(end.weekday() - weekday) % 7)


def _months(sessions: list[date]) -> list[tuple[int, int]]:
    return sorted({(d.year, d.month) for d in sessions})


def monthly_expiries(sessions: Iterable[date]) -> frozenset[date]:
    """The monthly F&O expiry session of each month the sessions cover through its rule date."""
    days = sorted(set(sessions))
    found: set[date] = set()
    for year, month in _months(days):
        rule = _last_weekday(year, month, _THURSDAY)
        if rule >= TUESDAY_EXPIRY_FROM:
            rule = _last_weekday(year, month, _TUESDAY)
        if not days or days[-1] < rule:
            continue
        held = [d for d in days if d <= rule and (d.year, d.month) == (year, month)]
        if held:
            found.add(held[-1])
    return frozenset(found)


def index_rebalance_closes(sessions: Iterable[date]) -> frozenset[date]:
    """The session before the last trading day of each March and September the sessions cover
    to its end (the close the index funds trade the reconstitution at)."""
    days = sorted(set(sessions))
    found: set[date] = set()
    for year, month in _months(days):
        if month not in _REBALANCE_MONTHS or days[-1] <= _month_end(year, month):
            continue
        held = [d for d in days if (d.year, d.month) == (year, month)]
        if len(held) >= 2:
            found.add(held[-2])
    return frozenset(found)


def auction_risk_days(sessions: Iterable[date]) -> Mapping[date, str]:
    """Each flagged session with its tags (`fo_expiry`, `index_rebalance`, comma-joined when
    both fall on it)."""
    days = sorted(set(sessions))
    tags: dict[date, list[str]] = {}
    for tag, found in (
        (FO_EXPIRY, monthly_expiries(days)),
        (INDEX_REBALANCE, index_rebalance_closes(days)),
    ):
        for day in found:
            tags.setdefault(day, []).append(tag)
    return {day: ",".join(sorted(t)) for day, t in sorted(tags.items())}
