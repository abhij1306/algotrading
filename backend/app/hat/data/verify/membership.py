"""D3: the stored NIFTY 500 membership (`hat_index_member`) against NSE's notices and archived
constituent lists.

- Size: the members of each session from the cover's start, grouped into runs of one count; a
  run off 500 warns with its extra or missing members named (a documented exception, such as the
  Tata Motors DVR counted beside its ordinary share until 2024-08-30, reads off them). Sessions
  of the window before the cover have no membership: a backtest there fails closed.
- Notices (`--notices`: `changes_parsed.json`, the NIFTY 500 sections parsed from NSE's index
  notices, and `overrides.json`, the hand-read corrections of revised or void notices): every
  stored entry after the cover's start and every exit must be a notice's inclusion or exclusion
  of that symbol (or of any symbol NSE's `symbolchange.csv` or HAT's symbol history joins it
  to) effective that session (or the first session after the notice's date), or an ISIN change
  of the same company (`hat_isin_lineage`, linked on that day).
- Lists: each archived constituent list (`raw/reference/nifty500/<YYYYMMDDhhmmss>_*.csv`, kept
  from the Wayback Machine's captures of NSE's and niftyindices' `ind_nifty500list.csv`, and
  today's list from NSE's archive, fetched with `--fetch`) against the stored members of its
  capture's session, by ISIN. A difference that is a demerger dummy or a security HAT has no
  instrument for warns, as does one within a session of a stored change (the capture may predate
  the file's update); any other fails.
"""

from __future__ import annotations

import csv
import io
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..audit import FAIL, WARN, check_result, finding
from ..nse_changes import SYMBOL_CHANGES_URL, parse_symbol_changes
from ..nse_files import INDUSTRY_LIST_URL
from .context import Context
from .references import reference_file

INDEX = "NIFTY 500"
SIZE = 500
LIST_GROUP = "nifty500"
# The Wayback Machine's capture index for the URLs NSE and niftyindices served the list under.
CDX_URL = (
    "https://web.archive.org/cdx/search/cdx?url={url}&output=json&filter=statuscode:200"
    "&collapse=digest"
)
LIST_URLS = (
    "nseindia.com/content/indices/ind_cnx500list.csv",
    "nseindia.com/content/indices/ind_nifty500list.csv",
    "niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
)
CAPTURE_URL = "https://web.archive.org/web/{stamp}id_/{original}"
DUMMY_PREFIX = "DUM"  # a demerger dummy's ISIN (DUM545A01024) and symbol (DUMMYHEG)


@dataclass(frozen=True)
class Member:
    instrument_id: str
    isin: str
    symbol: str
    start: date
    end: date | None  # the first session it is no longer a member


@dataclass(frozen=True)
class NoticeChange:
    file: str
    days: tuple[date, ...]  # the effective dates the notice names (any of them may apply)
    excluded: frozenset[str]
    included: frozenset[str]


def _day(raw: str) -> date:
    return date.fromisoformat(raw)


def notice_changes(
    parsed: Sequence[Mapping[str, Any]], overrides: Mapping[str, Any]
) -> list[NoticeChange]:
    """The NIFTY 500 changes of the parsed notices with the overrides applied: `skip` drops a
    notice, `date` names its one effective date, `changes` replaces its sections, `drop` removes
    symbols from another notice and `drop_from` removes from the named notice the symbols this
    one reschedules."""
    dropped: dict[str, set[str]] = defaultdict(set)
    sections: dict[str, tuple[set[str], set[str]]] = {}
    for item in parsed:
        excluded = {
            s[1].upper() for sec in item.get("sections", ()) for s in sec.get("excluded", ())
        }
        included = {
            s[1].upper() for sec in item.get("sections", ()) for s in sec.get("included", ())
        }
        sections[item["file"]] = (excluded, included)
    for name, rule in overrides.items():
        if not isinstance(rule, Mapping):
            continue
        for other, symbols in (rule.get("drop") or {}).items():
            dropped[other] |= {s.upper() for s in symbols}
        if rule.get("drop_from") and name in sections:
            dropped[rule["drop_from"]] |= set().union(*sections[name])
    out = []
    for item in parsed:
        name = item["file"]
        rule = overrides.get(name) or {}
        if rule.get("skip"):
            continue
        if rule.get("changes"):
            for change in rule["changes"]:
                out.append(
                    NoticeChange(
                        name,
                        (_day(change["date"]),),
                        frozenset(s.upper() for s in change.get("excluded", ())),
                        frozenset(s.upper() for s in change.get("included", ())),
                    )
                )
            continue
        days = (_day(rule["date"]),) if rule.get("date") else tuple(map(_day, item["effective"]))
        excluded, included = sections[name]
        gone = dropped.get(name, set())
        out.append(NoticeChange(name, days, frozenset(excluded - gone), frozenset(included - gone)))
    return out


def first_session(sessions: Sequence[date], day: date) -> date | None:
    at = bisect_left(sessions, day)
    return sessions[at] if at < len(sessions) else None


def trace_changes(
    members: Sequence[Member],
    notices: Sequence[NoticeChange],
    isin_switches: Collection[tuple[str, str, date]],
    sessions: Sequence[date],
    cover_start: date,
    aliases: Mapping[str, Collection[str]] | None = None,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Every stored entry after `cover_start` and every exit traced to a notice (its effective
    session) or an ISIN switch (old instrument, new instrument, day): (traced, untraced entries,
    untraced exits). A notice may name the company by any symbol it held (`aliases`, by
    instrument id), as a rename leaves the stored symbol and the notice's apart."""
    ins: dict[date, set[str]] = defaultdict(set)
    outs: dict[date, set[str]] = defaultdict(set)
    for n in notices:
        for day in n.days:
            effective = first_session(sessions, day)
            if effective is not None:
                ins[effective] |= n.included
                outs[effective] |= n.excluded
    switched_in = {(new, day) for _, new, day in isin_switches}
    switched_out = {(old, day) for old, _, day in isin_switches}
    traced, entries, exits = 0, [], []
    for m in members:
        names = {m.symbol.upper(), *(a.upper() for a in (aliases or {}).get(m.instrument_id, ()))}
        if m.start > cover_start:
            if names & ins.get(m.start, set()) or (m.instrument_id, m.start) in switched_in:
                traced += 1
            else:
                entries.append({"symbol": m.symbol, "isin": m.isin, "from": m.start.isoformat()})
        if m.end is not None:
            if names & outs.get(m.end, set()) or (m.instrument_id, m.end) in switched_out:
                traced += 1
            else:
                exits.append({"symbol": m.symbol, "isin": m.isin, "to": m.end.isoformat()})
    return traced, entries, exits


def members_on(members: Iterable[Member], day: date) -> list[Member]:
    return [m for m in members if m.start <= day and (m.end is None or day < m.end)]


def size_runs(members: Sequence[Member], sessions: Sequence[date]) -> list[dict[str, Any]]:
    """The sessions grouped into runs of one member count, each off-size run with the members
    that joined or left at its start against the run before (who makes the count)."""
    runs: list[dict[str, Any]] = []
    previous: set[str] = set()
    for day in sessions:
        today = members_on(members, day)
        names = {m.symbol for m in today}
        if runs and runs[-1]["count"] == len(today):
            runs[-1]["to"] = day.isoformat()
        else:
            runs.append(
                {
                    "from": day.isoformat(),
                    "to": day.isoformat(),
                    "count": len(today),
                    "joined": sorted(names - previous)[:10] if previous else [],
                    "left": sorted(previous - names)[:10],
                }
            )
        previous = names
    return runs


def read_list(text: str) -> dict[str, str]:
    """An NSE constituent list: ISIN -> symbol."""
    out = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        isin = row.get("ISIN Code", "").upper()
        if isin:
            out[isin] = row.get("Symbol", "").upper()
    return out


def compare_list(
    listed: Mapping[str, str],
    stored: Mapping[str, str],
    known_isins: Collection[str],
    near_change: Collection[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One archived list (ISIN -> symbol) against the stored members of its session (ISIN ->
    symbol): (explained differences, unexplained ones). A dummy, an ISIN HAT has no instrument
    for, or a symbol changed within a session of the capture (`near_change`) is explained."""
    explained, wrong = [], []
    for isin in sorted(set(listed) ^ set(stored)):
        symbol = listed.get(isin) or stored.get(isin, "")
        item = {"isin": isin, "symbol": symbol, "in": "list" if isin in listed else "store"}
        if isin.startswith(DUMMY_PREFIX) or symbol.startswith("DUMMY"):
            explained.append({**item, "why": "demerger dummy"})
        elif isin in listed and isin not in known_isins:
            explained.append({**item, "why": "no instrument in HAT"})
        elif isin in near_change or symbol in near_change:
            explained.append({**item, "why": "changed within a session of the capture"})
        else:
            wrong.append(item)
    return explained, wrong


def renamed_forms(changes: Iterable[tuple[str, str]], symbols: Collection[str]) -> set[str]:
    """Every symbol NSE's symbol changes join to `symbols`, either way, through any number of
    renames."""
    edges: dict[str, set[str]] = defaultdict(set)
    for old, new in changes:
        edges[old.upper()].add(new.upper())
        edges[new.upper()].add(old.upper())
    seen = {s.upper() for s in symbols}
    todo = list(seen)
    while todo:
        for nxt in edges.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def symbol_renames(ctx: Context) -> list[tuple[str, str]]:  # pragma: no cover - a file
    """NSE's symbol changes (`symbolchange.csv`, kept under `raw/reference/nse/`), or none."""
    data = reference_file(ctx, "nse", "symbolchange.csv", SYMBOL_CHANGES_URL)
    if data is None:
        ctx.note("D3: NSE symbolchange.csv not kept and not fetched; renames not followed")
        return []
    return [(c.old, c.new) for c in parse_symbol_changes(data.decode("utf-8-sig", "replace"))]


def capture_day(name: str) -> date | None:
    """`20181004105632_niftyindices.csv` -> its capture date."""
    stamp = name.split("_", 1)[0]
    try:
        return datetime.strptime(stamp[:8], "%Y%m%d").date()  # noqa: DTZ007 - a calendar date
    except ValueError:
        return None


def _fetch_lists(ctx: Context) -> None:  # pragma: no cover - the network
    """With `--fetch`: every Wayback capture of the list URLs (one per distinct content) and
    today's list from NSE's archive, kept under `raw/reference/nifty500/`."""
    for url in LIST_URLS:
        found = reference_file(
            ctx, LIST_GROUP, f"cdx-{url.replace('/', '_')}.json", CDX_URL.format(url=url)
        )
        try:
            rows = json.loads(found or b"[]")[1:]
        except ValueError:
            continue
        for _, stamp, original, *_ in rows:
            reference_file(
                ctx,
                LIST_GROUP,
                f"{stamp}_{url.split('/')[0].split('.')[0]}.csv",
                CAPTURE_URL.format(stamp=stamp, original=original),
                valid=lambda data: b"ISIN" in data[:300],
            )
    today = ctx.end.strftime("%Y%m%d")
    reference_file(
        ctx,
        LIST_GROUP,
        f"{today}000000_nsearchives-live.csv",
        INDUSTRY_LIST_URL,
        valid=lambda data: b"ISIN" in data[:300],
    )


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    cover = ctx.rows(
        "SELECT covered_from, covered_to FROM hat_index_membership WHERE index_name = :i", i=INDEX
    )
    sessions_all = [
        d for (d,) in ctx.rows("SELECT date FROM hat_trading_calendar WHERE is_trading ORDER BY 1")
    ]
    sessions = [d for d in sessions_all if ctx.start <= d <= ctx.end]
    if not cover:
        return check_result("D3", {}, [finding("no_membership", FAIL, [INDEX])])
    cover_start, cover_end = cover[0]
    members = [
        Member(str(i), isin, sym, a, b)
        for i, isin, sym, a, b in ctx.rows(
            "SELECT CAST(m.instrument_id AS TEXT), i.isin, m.symbol, m.effective_from, "
            "m.effective_to FROM hat_index_member m JOIN hat_instrument i "
            "ON i.id = m.instrument_id WHERE m.index_name = :i",
            i=INDEX,
        )
    ]
    covered = [d for d in sessions if cover_start <= d <= cover_end]
    uncovered = [d for d in sessions if d < cover_start or d > cover_end]
    findings = []
    if uncovered:
        findings.append(
            finding(
                "sessions_without_membership",
                WARN,
                [{"from": uncovered[0].isoformat(), "to": uncovered[-1].isoformat()}],
                sessions=len(uncovered),
            )
        )
    runs = size_runs(members, covered)
    off = [r for r in runs if r["count"] != SIZE]
    if off:
        findings.append(finding("member_count_not_500", WARN, off))
    summary: dict[str, Any] = {
        "cover": [cover_start.isoformat(), cover_end.isoformat()],
        "member_rows": len(members),
        "sessions_covered": len(covered),
        "count_runs": [{k: r[k] for k in ("from", "to", "count")} for r in runs],
    }
    switches = {
        (str(o), str(n), d)
        for o, n, d in ctx.rows(
            "SELECT old_instrument_id, new_instrument_id, effective_date FROM hat_isin_lineage "
            "WHERE status = 'linked'"
        )
    }
    in_window = [m for m in members if m.start <= ctx.end and (m.end is None or m.end > ctx.start)]
    if ctx.notices is not None and (ctx.notices / "changes_parsed.json").is_file():
        parsed = json.loads((ctx.notices / "changes_parsed.json").read_text(encoding="utf-8"))
        overrides_path = ctx.notices / "overrides.json"
        overrides = (
            json.loads(overrides_path.read_text(encoding="utf-8"))
            if overrides_path.is_file()
            else {}
        )
        notices = notice_changes(parsed, overrides)
        renames = symbol_renames(ctx)
        held: dict[str, set[str]] = defaultdict(set)  # HAT's own symbol history fills NSE's gaps
        for i, symbol in ctx.rows(
            "SELECT CAST(instrument_id AS TEXT), symbol FROM hat_instrument_symbol_history"
        ):
            held[i].add(symbol)
        aliases = {
            m.instrument_id: renamed_forms(renames, {m.symbol, *held[m.instrument_id]})
            for m in in_window
        }
        traced, entries, exits = trace_changes(
            in_window, notices, switches, sessions_all, cover_start, aliases
        )
        summary["notices"] = len(notices)
        summary["changes_traced"] = traced
        if entries:
            findings.append(finding("entry_without_notice", FAIL, entries))
        if exits:
            findings.append(finding("exit_without_notice", FAIL, exits))
    else:
        ctx.note("D3: no --notices folder with changes_parsed.json; changes not traced")
        findings.append(finding("notices_missing", WARN, ["changes_parsed.json"]))
    if ctx.fetch:
        _fetch_lists(ctx)
    known = {isin for (isin,) in ctx.rows("SELECT isin FROM hat_instrument")}
    lists = sorted(ctx.reference_dir(LIST_GROUP).glob("*.csv"))
    compared = []
    mismatched: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for path in lists:
        day = capture_day(path.name)
        at = None if day is None else first_session(sessions_all, day)
        if (
            day is None
            or at is None
            or not (max(cover_start, ctx.start) <= at <= min(cover_end, ctx.end))
        ):
            continue
        if at != day:  # captured on a closed day: the members of the session before
            at = sessions_all[bisect_left(sessions_all, day) - 1]
        listed = read_list(path.read_text(encoding="utf-8-sig", errors="replace"))
        stored = {m.isin: m.symbol for m in members_on(members, at)}
        lo = bisect_left(sessions_all, at) - 1
        near_days = set(sessions_all[max(lo, 0) : bisect_right(sessions_all, at) + 1])
        near = {
            key
            for m in members
            if m.start in near_days or m.end in near_days
            for key in (m.isin, m.symbol)
        }
        explained, wrong = compare_list(listed, stored, known, near)
        compared.append(
            {
                "file": path.name,
                "session": at.isoformat(),
                "listed": len(listed),
                "stored": len(stored),
                "explained": len(explained),
                "unexplained": len(wrong),
            }
        )
        mismatched += [{**item, "file": path.name} for item in wrong]
        differences += [{**item, "file": path.name} for item in explained]
    summary["lists_compared"] = compared
    if mismatched:
        findings.append(finding("list_mismatch", FAIL, mismatched))
    if differences:
        findings.append(finding("list_difference_explained", WARN, differences))
    if not compared:
        findings.append(finding("no_archived_list", WARN, [str(ctx.reference_dir(LIST_GROUP))]))
    return check_result("D3", summary, findings)


__all__ = [
    "Member",
    "NoticeChange",
    "compare_list",
    "notice_changes",
    "read_list",
    "run",
    "size_runs",
    "trace_changes",
]
