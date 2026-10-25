"""Point in time (CA5): when each stored corporate-action factor became knowable, and the days a
backtest must not trade a name on because its adjusted history shows a factor nobody had yet.

The adjusted series applies every factor to all earlier bars. A factor known at the ex-date open
(a split, a bonus or a rights issue priced from its terms, a dividend) changes nothing a trader
then could not have done. A factor known only later (a demerger stored at its market gap once the
gap held, a value only research or the owner supplied) hides the ex-date drop live would have
seen, so on the days from the ex-date up to its `known_on` the name is factor-pending
(`ca_versions.pending_days`, which reads `known_on` as the version's `available_on`): the
backtest's scan reads no bars of it and its entries skip it, while an open
position keeps its price-basis handling. Live is unchanged: it sees the raw drop.

`hat_corporate_action.known_on` is the last date whose data the factor rests on, so the first
date a backtest could have used it, and `known_basis` says which fact that was:

- `ex_open`: the ex-date, for a split, bonus or rights factor confirmed by (or priced at) the
  ex-date open, a dividend, and any price-neutral row;
- `settled_close`: the close `SETTLE_SESSIONS` sessions on (the ex session is the first), for a
  demerger stored at its market gap with no dated cross-check that agrees;
- `resulting_first_close`: the resulting company's first close, for a demerger whose factor is
  priced from it (CA1) or whose market gap a dated ratio cross-check confirms;
- `research_source_date`: the research row's `source_date`, never before the ex-date, for a value
  only research supplies;
- `correction`: the owner's correction line's `known_on`, never before the ex-date.

Both are NULL when nobody knows (a research row or a correction without a date, an MCP factor of
another type): the date is never guessed, and such a factor keeps its name factor-pending from
its ex-date on. A linked ISIN succession records the last bar its rules read
(`hat_isin_lineage.known_on`), reported beside the actions but not a pending window: the rules
read what bars there are, so a nightly build can link a chain after its first session.

The pending sessions and the lag report are `point_in_time`'s.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

EX_OPEN = "ex_open"
SETTLED_CLOSE = "settled_close"
RESULTING_FIRST_CLOSE = "resulting_first_close"
RESEARCH_SOURCE_DATE = "research_source_date"
CORRECTION = "correction"
BASES = (EX_OPEN, SETTLED_CLOSE, RESULTING_FIRST_CLOSE, RESEARCH_SOURCE_DATE, CORRECTION)

# A demerger's market gap must hold to this session's close (the ex session is the first) before
# a rule without an agreeing cross-check accepts it (`ca_research.market_factor`).
SETTLE_SESSIONS = 5


@dataclass(frozen=True)
class Known:
    """When a factor became knowable (`on`) and on what fact (`basis`); both None when nobody
    knows."""

    on: date | None = None
    basis: str | None = None


UNKNOWN = Known()


def at_ex_open(ex_date: date) -> Known:
    return Known(ex_date, EX_OPEN)


def from_sources(ex_date: date, dates: Sequence[date | None], basis: str) -> Known:
    """Known at the latest of `dates` (the sources the value rests on), never before the
    ex-date; unknown when there is no source or one has no date."""
    if not dates or any(d is None for d in dates):
        return UNKNOWN
    return Known(max(ex_date, *(d for d in dates if d is not None)), basis)


def settle_day(days: Sequence[date], ex_date: date) -> date | None:
    """The `SETTLE_SESSIONS`-th of the sorted sessions `days` on or after the ex-date, None when
    they do not reach it."""
    at = bisect_left(days, ex_date) + SETTLE_SESSIONS - 1
    return days[at] if at < len(days) else None


def settle_known(days: Sequence[date], ex_date: date) -> Known:
    day = settle_day(days, ex_date)
    return UNKNOWN if day is None else Known(day, SETTLED_CLOSE)


def earliest(routes: Iterable[Known]) -> Known:
    """The first dated route that accepts a factor; unknown when none is dated."""
    dated = [k for k in routes if k.on is not None]
    return min(dated, key=lambda k: k.on or date.max) if dated else UNKNOWN


def latest(parts: Iterable[Known]) -> Known:
    """When a value resting on all of `parts` became knowable: the latest part, unknown when one
    is (an amount summed from a feed dividend and a research distribution)."""
    found = list(parts)
    if not found or any(k.on is None for k in found):
        return UNKNOWN
    return max(found, key=lambda k: k.on or date.min)


def lag_sessions(days: Sequence[date], ex_date: date, known_on: date | None) -> int | None:
    """The sessions of `days` after the ex-date session up to `known_on` (0 when known at the
    ex-date open), None when unknown."""
    if known_on is None:
        return None
    return max(0, bisect_right(days, known_on) - bisect_right(days, ex_date))
