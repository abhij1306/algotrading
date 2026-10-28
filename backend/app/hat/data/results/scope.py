"""The results store's scope (owner, 2026-10-28): which instruments and which basis. Pure.

- Universe `nifty500` (the default): the NIFTY 500 point-in-time members, any membership in force
  on some day from the universe start. `ep`: the S2 EP universe (`universe.passing`).
- Basis `consolidated-first` (the default): for each company and quarter, its consolidated
  results; its standalone results only for a quarter it filed no consolidated results for (no
  subsidiaries), so that company still has data. `both`: every filing.
- The secondary source's standalone page is read only when the consolidated page has no
  quarters, and a standalone quarter from it only where no consolidated figure exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Protocol, TypeVar

UNIVERSE_NIFTY500 = "nifty500"
UNIVERSE_EP = "ep"
UNIVERSES = (UNIVERSE_NIFTY500, UNIVERSE_EP)
BASIS_CONSOLIDATED_FIRST = "consolidated-first"
BASIS_BOTH = "both"
BASIS_RULES = (BASIS_CONSOLIDATED_FIRST, BASIS_BOTH)
CONSOLIDATED = "consolidated"


class HasBasis(Protocol):
    @property
    def period_end(self) -> date: ...

    @property
    def basis(self) -> str: ...


T = TypeVar("T", bound=HasBasis)


def consolidated_first(items: Iterable[T], consolidated_elsewhere: Iterable[date] = ()) -> list[T]:
    """`items` (filings or quarters), keeping for each period end its consolidated items when
    there is one, here or in `consolidated_elsewhere` (period ends with a consolidated figure from
    another source), else all of that period's items (standalone, or basis unknown); in order."""
    listed = list(items)
    covered = set(consolidated_elsewhere) | {
        i.period_end for i in listed if i.basis == CONSOLIDATED
    }
    return [i for i in listed if i.basis == CONSOLIDATED or i.period_end not in covered]


def by_rule(items: Iterable[T], rule: str, consolidated_elsewhere: Iterable[date] = ()) -> list[T]:
    """`items` under the basis rule `rule` (`BASIS_RULES`)."""
    if rule == BASIS_BOTH:
        return list(items)
    return consolidated_first(items, consolidated_elsewhere)


def read_standalone(rule: str, consolidated_quarters: int | None) -> bool:
    """Whether to read the secondary source's standalone page beyond the consolidated one: always
    under `both`; under `consolidated-first` only when the consolidated page has no quarters
    (None: not read, or not found)."""
    return rule == BASIS_BOTH or not consolidated_quarters


def coverage_gap(covered_from: date | None, since: date) -> str | None:
    """Why the NIFTY 500 membership history cannot give the members from `since`: not loaded, or
    starting after it (members of the uncovered days would be silently missing); None when it
    can."""
    if covered_from is None:
        return "NIFTY 500 membership history not loaded"
    if covered_from > since:
        return f"NIFTY 500 membership history starts {covered_from}, after {since}"
    return None


def nifty500_first(
    memberships: Iterable[tuple[str, date, date | None]], since: date, until: date
) -> dict[str, date]:
    """Per instrument with a NIFTY 500 membership [effective_from, effective_to) in force on some
    day from `since` to `until`, the first such day."""
    first: dict[str, date] = {}
    for instrument_id, start, end in memberships:
        if start > until or (end is not None and end <= since):
            continue
        day = max(start, since)
        if instrument_id not in first or day < first[instrument_id]:
            first[instrument_id] = day
    return first
