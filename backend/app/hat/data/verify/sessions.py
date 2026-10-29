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
  successor's first bar. Every link must be proven by NSE (`link_unproven` fails otherwise): by
  a split, bonus or ETF unit action in NSE's corporate-action pulls (`--ca-data`) within
  `LINK_DAYS` of the link's date that names either ISIN or the successor's symbol
  (`ca_evidence`), or, for a rename, by NSE's symbol-change list (`symbolchange.csv`, kept under
  `verify_reference/nse/`, read here by `read_symbol_changes`). A link between two symbols needs
  the rename proven too: by the list, or by an action row naming the old ISIN under the new
  symbol (NSE files a company's actions under its later ticker). Links before the pulls start
  are judged by the list alone, and a fund's links (ETFs: stocks only) are counted, not
  judged. No list or no pulls fails, and rows of the list that cannot be
  read are counted (warn). A reviewed exception (`D6_EXCEPTIONS`: code, ISIN, day, reason)
  turns a finding on that ISIN and day into `excepted` (warn); one no finding uses warns.
- Listing (stocks only; an ETF, ISIN `INF...`, is counted, not judged): a stock the master
  calls inactive must not trade on the last session (`inactive_but_trading` fails: the live
  universe leaves it out); one called active should have traded in the window's last
  `STALE_SESSIONS` sessions (or be the old ISIN of a chain; warn: suspended or delisted).
- Suspensions: breaks inside a history (an instrument gone for more than
  `SUSPENSION_SESSIONS` sessions, then back) are counted by length, and the ones the series-break
  rule restarts the features at (`series_break.is_break`).
"""

from __future__ import annotations

import csv
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.paths import D6_EXCEPTIONS

from ..audit import FAIL, WARN, check_result, finding
from ..isin import ordinary_equity
from ..series_break import is_break
from ..store import DATASET_BARS
from . import dated
from .context import Context
from .exceptions import Exceptions, read_exceptions
from .references import (
    FOUND,
    NOT_FOUND,
    SYMBOL_CHANGES,
    SYMBOL_CHANGES_URL,
    probe_days,
    read_symbol_changes,
    reference_file,
)

LINK_DAYS = 10  # an action's ex-date this near a link's date proves it (record date, holidays)
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
    store's days, and NSE's archive asked about each weekday the calendar closes or has no row
    for (`probes`: `FOUND` or `NOT_FOUND`; a day missing from it is unproven). A weekday with
    no calendar row fails too. Every finding fails, dated."""

    def window(days: Collection[date]) -> set[date]:
        return {d for d in days if start <= d <= end}

    opened, closed = window(calendar_open), window(calendar_closed)
    files, stored = window(bhavcopy_days), window(store_days)
    unlisted = set(weekdays_between(start, end)) - opened - closed
    weekdays = {d for d in closed if d.weekday() < 5} | unlisted  # noqa: PLR2004 - Saturday is 5
    rules = (
        ("weekday_without_calendar_row", unlisted),
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


def weekdays_between(start: date, end: date) -> list[date]:
    """Monday to Friday from `start` to `end`."""
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return [d for d in days if d.weekday() < 5]  # noqa: PLR2004 - Saturday is 5


@dataclass(frozen=True)
class Link:
    old: str
    new: str
    effective: date
    reason: str
    old_symbol: str
    new_symbol: str
    old_isin: str = ""
    new_isin: str = ""
    chain: frozenset[str] = frozenset()  # every ISIN and symbol of the link's chain


@dataclass(frozen=True)
class Evidence:
    """One row of NSE's corporate-action pulls that can prove a link: its ex-date, ISIN and the
    symbol NSE files it under."""

    day: date
    isin: str
    symbol: str


def link_evidence(link: Link, evidence: Sequence[Evidence]) -> tuple[bool, bool]:
    """Whether NSE's action rows prove `link` (an action within `LINK_DAYS` of its date naming
    an ISIN or symbol of its chain: NSE files a company's past actions under its later ISIN and
    ticker), and whether one proves its rename (an ISIN of the chain filed under a later
    symbol of it)."""
    keys = link.chain | {link.old_isin, link.new_isin, link.new_symbol}
    near = [e for e in evidence if abs((e.day - link.effective).days) <= LINK_DAYS]
    proven = any(e.isin in keys or e.symbol in keys for e in near)
    rename = any(e.isin in keys and e.symbol in keys - {link.old_symbol} for e in near)
    return proven, rename


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
    evidence: Sequence[Evidence] | None = None,
    evidence_from: date | None = None,
    exceptions: Exceptions | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The linked chains: forks and loops fail, overlapping bars fail, a link not dated on its
    successor's first bar warns; a link NSE does not prove fails (`link_unproven`: no action
    row near it, `evidence`, from `evidence_from` on, and no listed rename), as does a rename
    neither the list nor an action row proves (`symbol_change_not_in_nse_list`); a fund's
    links are not judged by either (`is_fund`); no list
    (`symbol_changes` None) or no pulls (`evidence` None) fails. A reviewed exception
    (`exceptions`) takes its link out of the last two."""
    exceptions = exceptions or Exceptions()
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
    by_key: dict[str, list[Evidence]] = defaultdict(list)
    for e in evidence or ():
        by_key[e.isin].append(e)
        by_key[e.symbol].append(e)
    overlap, misdated, unlisted, no_bars, unproven = [], [], [], [], []
    judged = 0
    for link in links:
        old, new = spans.get(link.old), spans.get(link.new)
        item = {
            "old": link.old_symbol,
            "new": link.new_symbol,
            "isin": link.old_isin,
            "new_isin": link.new_isin,
            "effective": link.effective.isoformat(),
            "reason": link.reason,
        }
        if old is None or new is None:
            no_bars.append(item)  # still judged for its proof below
        else:
            if old[1] >= new[0]:
                overlap.append(
                    {**item, "old_last": old[1].isoformat(), "new_first": new[0].isoformat()}
                )
            if link.effective != new[0]:
                misdated.append({**item, "new_first": new[0].isoformat()})
        listed = symbol_changes is not None and renamed(
            symbol_changes, link.old_symbol, link.new_symbol
        )
        keys = link.chain | {link.old_isin, link.new_isin, link.new_symbol}
        near = [e for k in keys for e in by_key.get(k, ())]
        proven, rename = link_evidence(link, near)
        in_reference = (
            evidence is not None
            and not is_fund(link.old_isin)
            and (evidence_from is None or link.effective >= evidence_from)
        )
        if in_reference:
            judged += 1
            if not proven and not listed:
                unproven.append(item)
        if (
            symbol_changes is not None
            and not is_fund(link.old_isin)
            and link.old_symbol != link.new_symbol
            and not listed
            and not rename
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
    for code, found in (("symbol_change_not_in_nse_list", unlisted), ("link_unproven", unproven)):
        items = exceptions.split(code, found)
        if items:
            findings.append(dated(finding(code, FAIL, items), [i["effective"] for i in items]))
    if symbol_changes is None:
        findings.append(finding("symbol_changes_missing", FAIL, [SYMBOL_CHANGES]))
    if evidence is None:
        findings.append(finding("corporate_actions_missing", FAIL, ["ca-data"]))
    summary = {
        "links": len(links),
        "by_reason": dict(Counter(link.reason for link in links)),
        "symbol_change_list": None if symbol_changes is None else len(symbol_changes),
        "links_judged_by_actions": judged,
        "actions_from": None if evidence_from is None else evidence_from.isoformat(),
    }
    return summary, findings


@dataclass(frozen=True)
class Listing:
    instrument_id: str
    symbol: str
    active: bool
    delisted_on: date | None
    isin: str = ""


def is_fund(isin: str) -> bool:
    """An ETF or other fund unit (ISIN `INF...`): not a stock."""
    return isin.upper().startswith("INF")


def check_listing(
    listings: Sequence[Listing],
    last_bar: Mapping[str, date],
    sessions: Sequence[date],
    chained_old: Collection[str],
    exceptions: Exceptions | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The master's active flag against the bars, for stocks: an inactive stock with a bar on
    the last session fails (`inactive_but_trading`: a trading stock the live universe leaves
    out); an active one whose last bar is more than `STALE_SESSIONS` sessions before the last
    session warns (suspended or delisted but tradable to the engine), unless it is a chain's
    older ISIN. Anything but a stock (`isin.ordinary_equity`: funds, rights entitlements) is
    counted only."""
    if not sessions:
        return {}, []
    last = sessions[-1]
    stale_from = sessions[max(0, len(sessions) - STALE_SESSIONS)]
    stale, trading = [], []
    others = Counter()
    for item in listings:
        seen = last_bar.get(item.instrument_id)
        if seen is None:
            continue
        fund = not ordinary_equity(item.isin)
        if item.active and seen < stale_from and item.instrument_id not in chained_old:
            if fund:
                others["active_without_recent_bar"] += 1
            else:
                stale.append(
                    {"symbol": item.symbol, "isin": item.isin, "last_bar": seen.isoformat()}
                )
        if not item.active and seen == last:
            if fund:
                others["inactive_but_trading"] += 1
            else:
                trading.append(
                    {"symbol": item.symbol, "isin": item.isin, "last_bar": seen.isoformat()}
                )
    findings = []
    if stale:
        findings.append(finding("active_without_recent_bar", WARN, stale))
    trading = (exceptions or Exceptions()).split("inactive_but_trading", trading)
    if trading:
        findings.append(dated(finding("inactive_but_trading", FAIL, trading), [last]))
    summary = {
        "instruments_with_bars": len(last_bar),
        "inactive_without_delisted_on": sum(
            1
            for i in listings
            if not i.active and i.delisted_on is None and ordinary_equity(i.isin)
        ),
        "not_stocks_not_judged": dict(others),
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
    # every weekday the calendar does not open: closed, or with no row at all
    asked = set(weekdays_between(start, ctx.end)) - opened
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
        "l.effective_date, l.reason, o.symbol, n.symbol, o.isin, n.isin FROM hat_isin_lineage l "
        "JOIN hat_instrument o ON o.id = l.old_instrument_id "
        "JOIN hat_instrument n ON n.id = l.new_instrument_id WHERE l.status = 'linked'"
    )
    links = with_chains([Link(*r) for r in rows])
    data = reference_file(ctx, "nse", SYMBOL_CHANGES, SYMBOL_CHANGES_URL)
    changes, unread = None, 0
    if data is not None:
        renames, unread = read_symbol_changes(data)
        changes = [(r.old, r.new) for r in renames]
    evidence, evidence_from = None, None
    if ctx.ca_data is not None and ctx.ca_data.is_dir():
        evidence = read_evidence(ctx.ca_data) or None  # no row read: the reference is missing
        evidence_from = min((e.day for e in evidence or ()), default=None)
    exceptions = Exceptions(read_exceptions(D6_EXCEPTIONS, "D6"))
    link_summary, link_findings = check_links(
        links, spans, changes, evidence, evidence_from, exceptions
    )
    if unread:
        link_findings.append({**finding("symbol_changes_unread", WARN, []), "count": unread})
    listings = [
        Listing(str(i), s, a, d, isin)
        for i, s, a, d, isin in ctx.rows(
            "SELECT id, symbol, active, delisted_on, isin FROM hat_instrument"
        )
    ]
    in_window = [d for d in store_days if ctx.start <= d <= ctx.end]
    listing_summary, listing_findings = check_listing(
        listings,
        {i: s[1] for i, s in spans.items()},
        in_window,
        {link.old for link in links},
        exceptions,
    )
    return check_result(
        "D6",
        {
            "sessions": sessions_summary,
            "lineage": link_summary,
            "listing": listing_summary,
            "suspensions": gaps,
            "excepted": exceptions.summary(),
        },
        [*findings, *link_findings, *listing_findings, *exceptions.findings()],
    )


def with_chains(links: Sequence[Link]) -> list[Link]:
    """`links`, each with its chain's ISINs and symbols (`Link.chain`)."""
    from .adjustments import chains_of  # noqa: PLC0415

    keys: dict[str, set[str]] = defaultdict(set)
    for link in links:
        keys[link.old] |= {link.old_isin, link.old_symbol}
        keys[link.new] |= {link.new_isin, link.new_symbol}
    chain = chains_of(keys, [(link.old, link.new) for link in links])
    return [
        replace(link, chain=frozenset().union(*(keys[i] for i in chain[link.old])) - {""})
        for link in links
    ]


def read_evidence(folder: Path) -> list[Evidence]:  # pragma: no cover - files
    """The split, bonus and ETF unit-action rows of NSE's pulls in `folder`, as link evidence."""
    from .adjustments import ref_date  # noqa: PLC0415

    out = []
    for name, day_column in (
        ("splits_bonus_*.csv", "exDate"),
        ("etf_unit_actions_*.csv", "ex_effective_date"),
    ):
        for path in sorted(folder.glob(name)):
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    day = ref_date(row.get(day_column, ""))
                    if day is not None:
                        out.append(
                            Evidence(
                                day,
                                (row.get("isin") or "").strip().upper(),
                                (row.get("symbol") or "").strip().upper(),
                            )
                        )
    return out
