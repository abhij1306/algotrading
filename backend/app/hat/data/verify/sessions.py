"""D6: the trading calendar, the ISIN lineage chains and suspensions against NSE.

- Sessions: NSE writes a bhavcopy for every session and none for a holiday, so the files under
  `raw/` are the reference: a calendar session without a file, or a file on a day the calendar
  calls closed, fails, as does a session the store has no bars for. NSE's holiday lists as
  recorded (`nse_holidays_history.json`) are compared too: a listed holiday with a file, or a
  closed weekday the list does not name, warns.
- Lineage: the linked chains must not fork or loop; an older ISIN's bars must end before its
  successor's begin (else a chain has two bars a day) and the link's date must be the
  successor's first bar; a link between two symbols must be in NSE's symbol-change list
  (`symbolchange.csv`, kept under `raw/reference/nse/`).
- Suspensions: an instrument the master calls active must have traded in the window's last
  `STALE_SESSIONS` sessions (or be the old ISIN of a chain); one called inactive should not trade on
  the last session. Breaks inside a history (an instrument gone for more than
  `SUSPENSION_SESSIONS` sessions, then back) are counted by length, and the ones the series-break
  rule restarts the features at (`series_break.is_break`).
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..audit import FAIL, WARN, check_result, finding
from ..calendar import RECORDED_HISTORY
from ..nse_changes import SYMBOL_CHANGES_URL, parse_symbol_changes
from ..series_break import is_break
from ..store import DATASET_BARS
from .context import Context
from .references import reference_file

STALE_SESSIONS = 20  # an active instrument with no bar in this many sessions is suspended
SUSPENSION_SESSIONS = 5  # a hole this long inside a history counts as a suspension


def _iso(days: Iterable[date]) -> list[str]:
    return [d.isoformat() for d in sorted(days)]


def check_sessions(
    calendar_open: Collection[date],
    calendar_closed: Collection[date],
    bhavcopy_days: Collection[date],
    store_days: Collection[date],
    nse_holidays: Collection[date],
    start: date,
    end: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The calendar's sessions of `start..end` against the days NSE wrote a bhavcopy, the
    store's days and NSE's holiday list (`nse_holidays`, its years only)."""

    def window(days: Collection[date]) -> set[date]:
        return {d for d in days if start <= d <= end}

    opened, closed = window(calendar_open), window(calendar_closed)
    files, stored, listed = window(bhavcopy_days), window(store_days), window(nse_holidays)
    listed_years = {d.year for d in nse_holidays}
    findings = []
    rules = (
        ("session_without_bhavcopy", FAIL, opened - files),
        ("bhavcopy_on_closed_day", FAIL, files - opened),
        ("session_not_stored", FAIL, (opened & files) - stored),
        ("stored_day_not_session", FAIL, stored - opened),
        ("nse_holiday_with_bhavcopy", WARN, listed & files),
        (
            "closed_weekday_not_nse_holiday",
            WARN,
            {d for d in closed - listed if d.weekday() < 5 and d.year in listed_years} - files,
        ),
    )
    for code, severity, days in rules:
        if days:
            findings.append(finding(code, severity, _iso(days)))
    summary = {
        "calendar_sessions": len(opened),
        "bhavcopy_days": len(files),
        "store_days": len(stored),
        "weekend_sessions": len({d for d in opened if d.weekday() >= 5}),
    }
    return summary, findings


@dataclass(frozen=True)
class Link:
    old: str
    new: str
    effective: date
    reason: str
    old_symbol: str
    new_symbol: str


def renamed(changes: Iterable[tuple[str, str]], old: str, new: str) -> bool:
    """Whether NSE's symbol changes lead from `old` to `new`, through any number of renames."""
    edges: dict[str, set[str]] = defaultdict(set)
    for a, b in changes:
        edges[a].add(b)
    seen, todo = {old}, [old]
    while todo:
        for nxt in edges.get(todo.pop(), ()):
            if nxt == new:
                return True
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False


def check_links(
    links: Sequence[Link],
    spans: Mapping[str, tuple[date, date]],
    symbol_changes: Sequence[tuple[str, str]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The linked chains: forks and loops fail, overlapping bars fail, a link not dated on its
    successor's first bar warns, a symbol change NSE's list does not hold warns
    (`symbol_changes` None: the list was not available, which is said in the summary)."""
    succ: dict[str, list[str]] = defaultdict(list)
    pred: dict[str, list[str]] = defaultdict(list)
    for link in links:
        succ[link.old].append(link.new)
        pred[link.new].append(link.old)
    forks = sorted(k for k, v in (*succ.items(), *pred.items()) if len(v) > 1)
    loops = []
    for start in succ:
        seen, at = {start}, start
        while succ.get(at):
            at = succ[at][0]
            if at in seen:
                loops.append(start)
                break
            seen.add(at)
    overlap, dated, unlisted, no_bars = [], [], [], []
    for link in links:
        old, new = spans.get(link.old), spans.get(link.new)
        item = {
            "old": link.old_symbol,
            "new": link.new_symbol,
            "effective": link.effective.isoformat(),
            "reason": link.reason,
        }
        if old is None or new is None:
            no_bars.append(item)
            continue
        if old[1] >= new[0]:
            overlap.append(
                {**item, "old_last": old[1].isoformat(), "new_first": new[0].isoformat()}
            )
        if link.effective != new[0]:
            dated.append({**item, "new_first": new[0].isoformat()})
        if (
            symbol_changes is not None
            and link.old_symbol != link.new_symbol
            and not renamed(symbol_changes, link.old_symbol, link.new_symbol)
        ):
            unlisted.append(item)
    findings = []
    for code, severity, items in (
        ("chain_fork", FAIL, forks),
        ("chain_loop", FAIL, loops),
        ("chain_bars_overlap", FAIL, overlap),
        ("link_without_bars", WARN, no_bars),
        ("link_not_on_first_bar", WARN, dated),
        ("symbol_change_not_in_nse_list", WARN, unlisted),
    ):
        if items:
            findings.append(finding(code, severity, items))
    summary = {
        "links": len(links),
        "by_reason": dict(Counter(link.reason for link in links)),
        "symbol_change_list": None if symbol_changes is None else len(symbol_changes),
    }
    return summary, findings


@dataclass(frozen=True)
class Listing:
    instrument_id: str
    symbol: str
    active: bool
    delisted_on: date | None


def check_listing(
    listings: Sequence[Listing],
    last_bar: Mapping[str, date],
    sessions: Sequence[date],
    chained_old: Collection[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The master's active flag against the bars: an active instrument whose last bar is more
    than `STALE_SESSIONS` sessions before the last session warns (suspended or delisted but
    tradable to the engine), unless it is a chain's older ISIN; an inactive one with a bar on the
    last session warns (a trading stock the live universe leaves out; the backtest reads index
    membership, not this flag)."""
    if not sessions:
        return {}, []
    last = sessions[-1]
    stale_from = sessions[max(0, len(sessions) - STALE_SESSIONS)]
    stale, trading = [], []
    for item in listings:
        seen = last_bar.get(item.instrument_id)
        if seen is None:
            continue
        if item.active and seen < stale_from and item.instrument_id not in chained_old:
            stale.append({"symbol": item.symbol, "last_bar": seen.isoformat()})
        if not item.active and seen == last:
            trading.append({"symbol": item.symbol, "delisted_on": str(item.delisted_on)})
    findings = []
    if stale:
        findings.append(finding("active_without_recent_bar", WARN, stale))
    if trading:
        findings.append(finding("inactive_but_trading", WARN, trading))
    summary = {
        "instruments_with_bars": len(last_bar),
        "inactive_without_delisted_on": sum(
            1 for i in listings if not i.active and i.delisted_on is None
        ),
    }
    return summary, findings


def suspension_gaps(
    bar_days: Iterable[tuple[str, Sequence[date]]], sessions: Sequence[date]
) -> dict[str, Any]:
    """Holes of more than `SUSPENSION_SESSIONS` sessions inside each instrument's bars (its
    `bar_days`, sorted), by length, and how many of them the series-break rule restarts at."""
    buckets: Counter[str] = Counter()
    breaks = 0
    longest: list[tuple[int, str, str]] = []
    for instrument, days in bar_days:
        for a, b in zip(days, days[1:], strict=False):
            gap = bisect_left(sessions, b) - bisect_left(sessions, a) - 1
            if gap <= SUSPENSION_SESSIONS:
                continue
            bucket = "6-20" if gap <= 20 else "21-62" if gap <= 62 else "63+"
            buckets[bucket] += 1
            breaks += is_break(a, b)
            longest.append((gap, instrument, a.isoformat()))
    longest.sort(reverse=True)
    return {
        "gaps_by_sessions": dict(buckets),
        "series_breaks": breaks,
        "longest": [{"sessions": g, "instrument": i, "after": d} for g, i, d in longest[:10]],
    }


_SPANS_SQL = """
SELECT CAST(instrument_id AS VARCHAR), min(date), max(date) FROM bars_daily_raw GROUP BY 1
"""
_BAR_DAYS_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS i, list(date ORDER BY date) FROM bars_daily_raw
WHERE date BETWEEN ? AND ? GROUP BY 1
"""


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    from importlib import resources  # noqa: PLC0415

    from .rawbars import find_raw_days  # noqa: PLC0415

    calendar = ctx.rows("SELECT date, is_trading FROM hat_trading_calendar")
    opened = {d for d, t in calendar if t}
    closed = {d for d, t in calendar if not t}
    recorded = json.loads(
        resources.files("app.hat.data").joinpath(RECORDED_HISTORY).read_text(encoding="utf-8")
    )
    holidays = {date.fromisoformat(d) for year in recorded["holidays"].values() for d in year}
    files = set(find_raw_days(ctx, ctx.start))
    store_days = ctx.store.days(DATASET_BARS)
    sessions_summary, findings = check_sessions(
        opened, closed, files, store_days, holidays, ctx.start, ctx.end
    )
    with ctx.store.connect() as con:
        spans = {i: (a, b) for i, a, b in con.execute(_SPANS_SQL).fetchall()}
        gaps = suspension_gaps(
            ((i, days) for i, days in con.execute(_BAR_DAYS_SQL, [ctx.start, ctx.end]).fetchall()),
            [d for d in store_days if ctx.start <= d <= ctx.end],
        )
    rows = ctx.rows(
        "SELECT CAST(l.old_instrument_id AS TEXT), CAST(l.new_instrument_id AS TEXT), "
        "l.effective_date, l.reason, o.symbol, n.symbol FROM hat_isin_lineage l "
        "JOIN hat_instrument o ON o.id = l.old_instrument_id "
        "JOIN hat_instrument n ON n.id = l.new_instrument_id WHERE l.status = 'linked'"
    )
    links = [Link(*r) for r in rows]
    data = reference_file(ctx, "nse", "symbolchange.csv", SYMBOL_CHANGES_URL)
    changes = (
        None
        if data is None
        else [(c.old, c.new) for c in parse_symbol_changes(data.decode("utf-8-sig", "replace"))]
    )
    if changes is None:
        ctx.note("D6: NSE symbolchange.csv not kept and not fetched; symbol links unchecked")
    link_summary, link_findings = check_links(links, spans, changes)
    listings = [
        Listing(str(i), s, a, d)
        for i, s, a, d in ctx.rows("SELECT id, symbol, active, delisted_on FROM hat_instrument")
    ]
    in_window = [d for d in store_days if ctx.start <= d <= ctx.end]
    listing_summary, listing_findings = check_listing(
        listings, {i: s[1] for i, s in spans.items()}, in_window, {link.old for link in links}
    )
    return check_result(
        "D6",
        {
            "sessions": sessions_summary,
            "lineage": link_summary,
            "listing": listing_summary,
            "suspensions": gaps,
        },
        [*findings, *link_findings, *listing_findings],
    )
