"""D6: the trading calendar, the ISIN lineage chains and suspensions against NSE.

- Sessions: NSE writes a bhavcopy for every session and none for a holiday. A calendar session
  without a file under `raw/`, or a file on a day the calendar calls closed, fails, as does a
  session the store has no bars for. The files under `raw/` were fetched for the calendar's
  sessions, so a trading day the calendar wrongly closed would have none: every weekday the
  calendar closes is asked of NSE's archive itself (`references.probe_days`; the answers are
  kept). An archive file for it fails (`closed_weekday_has_bhavcopy`), and so does a closed
  weekday not asked yet (`closed_weekday_unproven`, run with `--fetch`). The holiday list the
  calendar is built from is not a reference here.
- Lineage: the linked chains must not fork or loop; an older ISIN's bars must end before its
  successor's begin (else a chain has two bars a day) and the link's date must be the
  successor's first bar; a link between two symbols must be in NSE's symbol-change list
  (`symbolchange.csv`, kept under `raw/reference/nse/`, read here by `read_symbol_changes`),
  else it fails; no list fails too, and rows of it that cannot be read are counted (warn).
- Suspensions: an instrument the master calls active must have traded in the window's last
  `STALE_SESSIONS` sessions (or be the old ISIN of a chain); one called inactive should not trade on
  the last session. Breaks inside a history (an instrument gone for more than
  `SUSPENSION_SESSIONS` sessions, then back) are counted by length, and the ones the series-break
  rule restarts the features at (`series_break.is_break`).
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..audit import FAIL, WARN, check_result, finding
from ..series_break import is_break
from ..store import DATASET_BARS
from . import dated
from .context import Context
from .references import (
    FOUND,
    NOT_FOUND,
    SYMBOL_CHANGES,
    SYMBOL_CHANGES_URL,
    probe_days,
    read_symbol_changes,
    reference_file,
)

STALE_SESSIONS = 20  # an active instrument with no bar in this many sessions is suspended
SUSPENSION_SESSIONS = 5  # a hole this long inside a history counts as a suspension


def _iso(days: Iterable[date]) -> list[str]:
    return [d.isoformat() for d in sorted(days)]


def check_sessions(  # noqa: PLR0913
    calendar_open: Collection[date],
    calendar_closed: Collection[date],
    bhavcopy_days: Collection[date],
    store_days: Collection[date],
    probes: Mapping[date, str],
    start: date,
    end: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The calendar's sessions of `start..end` against the days a bhavcopy is under `raw/`, the
    store's days, and NSE's archive asked about each weekday the calendar closes (`probes`:
    `FOUND` or `NOT_FOUND`; a day missing from it is unproven). Every finding fails, dated."""

    def window(days: Collection[date]) -> set[date]:
        return {d for d in days if start <= d <= end}

    opened, closed = window(calendar_open), window(calendar_closed)
    files, stored = window(bhavcopy_days), window(store_days)
    weekdays = {d for d in closed if d.weekday() < 5}  # noqa: PLR2004 - Saturday is 5
    rules = (
        ("session_without_bhavcopy", opened - files),
        ("bhavcopy_on_closed_day", files - opened),
        ("session_not_stored", (opened & files) - stored),
        ("stored_day_not_session", stored - opened),
        ("closed_weekday_has_bhavcopy", {d for d in weekdays if probes.get(d) == FOUND}),
        ("closed_weekday_unproven", {d for d in weekdays if d not in probes}),
    )
    findings = [dated(finding(code, FAIL, _iso(days)), days) for code, days in rules if days]
    summary = {
        "calendar_sessions": len(opened),
        "bhavcopy_days": len(files),
        "store_days": len(stored),
        "weekend_sessions": len({d for d in opened if d.weekday() >= 5}),  # noqa: PLR2004
        "closed_weekdays": len(weekdays),
        "closed_weekdays_without_archive_file": sum(probes.get(d) == NOT_FOUND for d in weekdays),
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
    successor's first bar warns, a symbol change NSE's list does not hold fails, as does no list
    (`symbol_changes` None)."""
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
    overlap, misdated, unlisted, no_bars = [], [], [], []
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
            misdated.append({**item, "new_first": new[0].isoformat()})
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
        ("link_not_on_first_bar", WARN, misdated),
    ):
        if items:
            findings.append(finding(code, severity, items))
    if unlisted:
        found = finding("symbol_change_not_in_nse_list", FAIL, unlisted)
        findings.append(dated(found, [i["effective"] for i in unlisted]))
    if symbol_changes is None:
        findings.append(finding("symbol_changes_missing", FAIL, [SYMBOL_CHANGES]))
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
    from ..bhavcopy import FIRST_ISIN_DAY  # noqa: PLC0415
    from .rawbars import find_raw_days  # noqa: PLC0415

    start = max(ctx.start, FIRST_ISIN_DAY)
    calendar = ctx.rows("SELECT date, is_trading FROM hat_trading_calendar")
    opened = {d for d, t in calendar if t}
    closed = {d for d, t in calendar if not t}
    files = set(find_raw_days(ctx, start))
    store_days = ctx.store.days(DATASET_BARS)
    asked = {d for d in closed if start <= d <= ctx.end and d.weekday() < 5}  # noqa: PLR2004
    probes = probe_days(ctx, asked)
    sessions_summary, findings = check_sessions(
        opened, closed, files, store_days, probes, start, ctx.end
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
    data = reference_file(ctx, "nse", SYMBOL_CHANGES, SYMBOL_CHANGES_URL)
    changes, unread = None, 0
    if data is not None:
        renames, unread = read_symbol_changes(data)
        changes = [(r.old, r.new) for r in renames]
    link_summary, link_findings = check_links(links, spans, changes)
    if unread:
        link_findings.append({**finding("symbol_changes_unread", WARN, []), "count": unread})
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
