"""The rule-based research universe `TOP_TRADED` (a cross-check of the NIFTY 500's point-in-time
membership; `--universe TOP_TRADED`, or `TOP_TRADED_<N>`): the `N` (default 500) NSE stocks with
the highest median daily traded value over the `SESSIONS` market sessions through a reform
night, re-formed on the last session of March and of September, each list a member from the
next session to the next list's (`members`).

Computed from the daily bars alone (`data.top_traded_members` reads them), so a stock that later
left the market is in the lists it traded into: no survivor list is read. A candidate is an
ordinary-equity NSE stock (`data.nse_stocks`, as `NSE_STOCKS`) that traded in the `EQ` series
(an `EQ` bar with a positive traded value; a bar of zero traded value is no trade) on one of the
`RECENT_SESSIONS` sessions through the reform night; its median runs over all
`SESSIONS` sessions, a session without an `EQ` bar counting as no trade (zero), so a new listing
or a suspended stock ranks on what it traded. Ties go to the lower id. The universe has no stored
row: its version id carries `N` (`version_id`, `count_of`), so a rerun of a run's `config.json`
reads the same rule.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

import numpy as np

__all__ = [
    "DEFAULT_COUNT",
    "NAME",
    "RECENT_SESSIONS",
    "REFORM_MONTHS",
    "SESSIONS",
    "UNIVERSE_ID",
    "by_name",
    "count_of",
    "count_of_name",
    "intervals",
    "label",
    "reform_nights",
    "rule",
    "top_names",
    "version_id",
    "window_of",
]

NAME = "TOP_TRADED"
DEFAULT_COUNT = 500
MAX_COUNT = 2000
SESSIONS = 126  # the trailing market sessions of the median traded value
RECENT_SESSIONS = 5  # a candidate traded in EQ on one of these, through the reform night
REFORM_MONTHS = (3, 9)  # re-formed on the last session of March and of September
UNIVERSE_ID = uuid.UUID("01a18d00-0000-7000-8000-000000000003")
_VERSION_PREFIX = "01a18d00-0000-7000-8001-"  # then `N` in 12 decimal digits
_NAMED = re.compile(rf"^{NAME}(?:_(\d{{1,4}}))?$")


def count_of_name(text: str | None) -> int | None:
    """`N` of a `--universe` naming the rule (`TOP_TRADED` 500, `TOP_TRADED_<N>`), None for any
    other name; a count outside 1..`MAX_COUNT` is refused (ValueError)."""
    found = _NAMED.match(text or "")
    if found is None:
        return None
    count = DEFAULT_COUNT if found.group(1) is None else int(found.group(1))
    if not 1 <= count <= MAX_COUNT:
        raise ValueError("backtest_top_traded_count")
    return count


def version_id(count: int) -> uuid.UUID:
    """The universe version id of the rule with `count` names (never stored)."""
    return uuid.UUID(f"{_VERSION_PREFIX}{count:012d}")


def count_of(version: uuid.UUID) -> int | None:
    """`N` of a `version_id`, None for any other universe version."""
    text = str(version)
    if not text.startswith(_VERSION_PREFIX):
        return None
    tail = text[len(_VERSION_PREFIX) :]
    return int(tail) if tail.isdigit() and 1 <= int(tail) <= MAX_COUNT else None


def label(count: int) -> str:
    """The run's universe label (`run.json` `data.universe`, the trial family's)."""
    return f"{NAME}_{count}"


def rule(count: int) -> dict[str, object]:
    """The rule as a run records it (`config.json` `data_snapshot.membership.rule`, run.json
    `data.universe_rule`)."""
    return {
        "name": NAME,
        "count": count,
        "sessions": SESSIONS,
        "recent_sessions": RECENT_SESSIONS,
        "reform_months": list(REFORM_MONTHS),
        "series": "EQ",
    }


def reform_nights(days: Sequence[date], since: date, until: date) -> list[date]:
    """The reform nights a run of `since..until` reads, from the market sessions `days` (in
    order): each session that is the last of a March or September (the next session is in
    another month), from the last one on or before `since` through `until`; the last session of
    `days` is never one (its month's end cannot be judged). Empty when no reform night falls on
    or before `since`."""
    nights = [
        night
        for night, after in zip(days, days[1:], strict=False)
        if night.month in REFORM_MONTHS and after.month != night.month
    ]
    before = [n for n in nights if n <= since]
    if not before:
        return []
    return [before[-1], *(n for n in nights if since < n <= until)]


def top_names(
    values: Mapping[str, Mapping[date, float]],
    sessions: Sequence[date],
    count: int,
    recent: int = RECENT_SESSIONS,
) -> list[str]:
    """The `count` names with the highest median traded value over `sessions` (the window
    through a reform night, oldest first), best first, ties by name: `values` each candidate's
    EQ traded value by session, a session without one counting as zero; a candidate needs a
    positive value (a trade, not merely a bar) on one of the last `recent` sessions."""
    tail = sessions[-recent:]
    ranked: list[tuple[float, str]] = []
    for name, by_day in values.items():
        if not any(by_day.get(day, 0.0) > 0 for day in tail):
            continue
        series = np.array([by_day.get(day, 0.0) or 0.0 for day in sessions], dtype=np.float64)
        ranked.append((-float(np.median(series)), name))
    ranked.sort()
    return [name for _, name in ranked[:count]]


def intervals(
    lists: Sequence[tuple[date, Sequence[str]]], days: Sequence[date]
) -> list[tuple[str, date, date | None]]:
    """The membership spans of the reform `lists` ((reform night, its names), oldest first),
    as (name, first member session, the session it left or None): each list a member from the
    session after its night to the session after the next list's night, the last one open; a
    name on consecutive lists is one span. `days` are the market sessions (each night has a
    next one)."""

    def after(night: date) -> date:
        at = list(days).index(night)
        return days[at + 1]

    out: list[tuple[str, date, date | None]] = []
    open_: dict[str, date] = {}
    for night, names in lists:
        start = after(night)
        current = set(names)
        for name in sorted(open_.keys() - current):
            out.append((name, open_.pop(name), start))
        for name in sorted(current - open_.keys()):
            open_[name] = start
    out.extend((name, first, None) for name, first in sorted(open_.items()))
    return sorted(out, key=lambda span: (span[0], span[1]))


def window_of(days: Sequence[date], night: date, sessions: int = SESSIONS) -> list[date]:
    """The `sessions` market sessions of `days` through `night`; ValueError
    (`backtest_top_traded_history_short`) when `days` hold fewer."""
    at = list(days).index(night)
    if at + 1 < sessions:
        raise ValueError("backtest_top_traded_history_short")
    return list(days[at + 1 - sessions : at + 1])


def by_name(rows: Iterable[tuple[str, date, float | None]]) -> dict[str, dict[date, float]]:
    """(name, session, traded value) rows as each name's values by session; a None value is
    no trade."""
    out: dict[str, dict[date, float]] = {}
    for name, day, value in rows:
        out.setdefault(str(name), {})[day] = 0.0 if value is None else float(value)
    return out
