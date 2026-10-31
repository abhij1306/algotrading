"""D3: the stored NIFTY 500 membership (`hat_index_member`) against NSE's notices and archived
constituent lists.

- Scope: a backtest reads membership only from the night before its first session, and refuses
  a start before the history (`backtest.data.membership_cover_refusal`); its lookback reads bars
  and features, which rank every ordinary equity, never the index. So the history must start by
  `NEEDED_FROM` (decisions.md "NIFTY 500 history from 2011-06-22"; a later start fails), and the
  sessions before its start are listed (warn), not failed. A session after its end fails (dated:
  a backtest there has no point-in-time universe).
- Size: the members of each session from the cover's start, grouped into runs of one count; a
  run off 500 warns with its extra or missing members named (a documented exception, such as the
  Tata Motors DVR counted beside its ordinary share until 2024-08-30, reads off them).
- Notices (`--notices`: `changes_parsed.json`, the NIFTY 500 sections parsed from NSE's index
  notices, and `overrides.json`, the hand-read corrections of revised or void notices), both
  ways. Every stored entry after the cover's start and every exit must be a notice's inclusion
  or exclusion of that symbol (or of any symbol NSE's `symbolchange.csv`, read here, joins it
  to) effective that session (or the first session after the notice's date), or one half of an
  ISIN change: the same symbol leaving under one ISIN and entering under another that session.
  And every notice's inclusion and exclusion effective in the cover must be applied in the store
  on that session. Each traced change cites its notice. No notices, or no symbol-change file,
  fails.
- Lists: each archived constituent list (`verify_reference/nifty500/<YYYYMMDDhhmmss>_*.csv`, kept
  from the Wayback Machine's captures of NSE's and niftyindices' `ind_nifty500list.csv`, and
  today's list from NSE's archive, fetched with `--fetch`) against the stored members of its
  capture's session (`list_session`: the last session closed by the capture's UTC stamp), by
  ISIN. The membership holds every constituent NSE publishes that HAT has an instrument for,
  REIT and InvIT units included (universes drop them by ISIN: decisions.md "Stocks only"). A
  difference that is a demerger dummy warns, as does one within a session of a stored change
  (the capture may predate the file's update) and a listed REIT or InvIT unit HAT has no
  instrument for (the instrument scan reads no RR or IV series row); any other fails, a listed
  member HAT has no instrument for among them (survivorship). No list fails.
- Exceptions: a failed change or list item the reviewed list names (`VERIFY_EXCEPTIONS["d3"]`:
  code, ISIN, day, pin, reason) warns as `excepted` (`exceptions.py`). A notice names a symbol,
  so its item takes the one ISIN that symbol has had (`isin_of_symbol`: the instruments, their
  former symbols and the archived lists), or none when it has had two.
"""

from __future__ import annotations

import csv
import io
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.paths import VERIFY_EXCEPTIONS

from ...time import IST
from ..audit import FAIL, WARN, check_result, finding
from ..isin import trust_unit
from ..nse_files import INDUSTRY_LIST_URL
from ..sessions import SESSION_CLOSE
from . import dated
from .context import Context
from .exceptions import Exceptions, read_exceptions
from .references import SYMBOL_CHANGES, SYMBOL_CHANGES_URL, read_symbol_changes, reference_file

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
LIVE_SUFFIX = "_nsearchives-live.csv"
# [A] decisions.md "NIFTY 500 history from 2011-06-22": the first session a backtest may read
# members on
NEEDED_FROM = date(2011, 6, 22)


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


def list_session(sessions: Sequence[date], at: datetime) -> date | None:
    """The session a list captured at `at` shows: the last session closed (15:30 IST) by then.
    NSE rewrites the file after a session, so a capture in the night before a change's
    effective session, or during it, still holds the members before it; None before the first
    session. The window is judged on this session, never on the next one."""
    i = bisect_right(sessions, at.astimezone(IST).date())
    while i and datetime.combine(sessions[i - 1], SESSION_CLOSE, tzinfo=IST) > at:
        i -= 1
    return sessions[i - 1] if i else None


def first_session(sessions: Sequence[date], day: date) -> date | None:
    at = bisect_left(sessions, day)
    return sessions[at] if at < len(sessions) else None


@dataclass
class Trace:
    """The changes traced both ways: each traced stored change with what it cites, the stored
    entries and exits no notice or ISIN change explains, and the notices' changes the store did
    not apply on their session."""

    traced: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    exits: list[dict[str, Any]]
    unapplied: list[dict[str, Any]]


def _isin_changes(
    members: Sequence[Member], names: Mapping[str, set[str]]
) -> dict[tuple[str, date], str]:
    """The exits and entries that are one company's ISIN change: on one session a member leaves
    and another of a different ISIN enters under a symbol they share. (instrument, day) -> the
    other ISIN."""
    entering: dict[date, list[Member]] = defaultdict(list)
    for m in members:
        entering[m.start].append(m)
    found = {}
    for m in members:
        if m.end is None:
            continue
        for n in entering.get(m.end, ()):
            if n.isin != m.isin and names[m.instrument_id] & names[n.instrument_id]:
                found[(m.instrument_id, m.end)] = n.isin
                found[(n.instrument_id, n.start)] = m.isin
    return found


def _effective(
    notices: Sequence[NoticeChange], sessions: Sequence[date]
) -> tuple[dict[date, dict[str, str]], dict[date, dict[str, str]]]:
    """The notices' inclusions and exclusions by effective session: symbol -> notice file."""
    ins: dict[date, dict[str, str]] = defaultdict(dict)
    outs: dict[date, dict[str, str]] = defaultdict(dict)
    for n in notices:
        for day in n.days:
            effective = first_session(sessions, day)
            if effective is not None:
                ins[effective].update(dict.fromkeys(n.included, n.file))
                outs[effective].update(dict.fromkeys(n.excluded, n.file))
    return ins, outs


def _unapplied(
    members: Sequence[Member],
    names: Mapping[str, set[str]],
    notices: Sequence[NoticeChange],
    sessions: Sequence[date],
    span: tuple[date, date],
) -> list[dict[str, Any]]:
    """Each notice inclusion (exclusion) effective in `span` with no member of that symbol
    entering (leaving) on that session; a demerger dummy (`DUMMY...`, never stored) is not one."""
    starts: dict[date, set[str]] = defaultdict(set)
    ends: dict[date, set[str]] = defaultdict(set)
    for m in members:
        starts[m.start] |= names[m.instrument_id]
        if m.end is not None:
            ends[m.end] |= names[m.instrument_id]
    out = []
    for n in notices:
        days = [
            e
            for d in n.days
            if (e := first_session(sessions, d)) is not None and span[0] <= e <= span[1]
        ]
        if not days:
            continue
        for kind, symbols, applied in (
            ("inclusion", n.included, starts),
            ("exclusion", n.excluded, ends),
        ):
            out += [
                {"notice": n.file, "symbol": s, "kind": kind, "day": days[0].isoformat()}
                for s in sorted(symbols)
                if not s.startswith("DUMMY") and not any(s in applied.get(e, ()) for e in days)
            ]
    return out


def trace_changes(  # noqa: PLR0913
    members: Sequence[Member],
    notices: Sequence[NoticeChange],
    sessions: Sequence[date],
    cover: tuple[date, date],
    window: tuple[date, date],
    aliases: Mapping[str, Collection[str]] | None = None,
) -> Trace:
    """Every stored entry after the cover's start and every exit, in `window`, traced to a
    notice (its effective session) or an ISIN change (`_isin_changes`); and every notice change
    effective after the cover's start, in the cover and the window, found applied in the store
    that session. A notice may name the company by any symbol NSE's renames join to it
    (`aliases`, by instrument id)."""
    ins, outs = _effective(notices, sessions)
    names = {
        m.instrument_id: {
            m.symbol.upper(),
            *(a.upper() for a in (aliases or {}).get(m.instrument_id, ())),
        }
        for m in members
    }
    switches = _isin_changes(members, names)
    first, last = window
    out = Trace([], [], [], [])
    for m in members:
        own = names[m.instrument_id]
        for day, table, kind, misses in (
            (m.start, ins, "entry", out.entries),
            (m.end, outs, "exit", out.exits),
        ):
            if day is None or not first <= day <= last or (kind == "entry" and day <= cover[0]):
                continue
            item = {"symbol": m.symbol, "isin": m.isin, "day": day.isoformat(), "kind": kind}
            cited = sorted(own & set(table.get(day, {})))
            if cited:
                out.traced.append({**item, "notice": table[day][cited[0]], "as": cited[0]})
            elif (m.instrument_id, day) in switches:
                out.traced.append({**item, "isin_change": switches[(m.instrument_id, day)]})
            else:
                misses.append(item)
    span = (max(first, cover[0] + timedelta(days=1)), min(last, cover[1]))
    out.unapplied = _unapplied(members, names, notices, sessions, span)
    return out


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
    symbol): (explained differences, unexplained ones). A dummy, or a symbol changed within a
    session of the capture (`near_change`), is explained, as is a listed REIT or InvIT unit HAT
    has no instrument for (no universe holds one); any other listed ISIN HAT has no instrument
    for is not (the universe misses a member)."""
    explained, wrong = [], []
    for isin in sorted(set(listed) ^ set(stored)):
        symbol = listed.get(isin) or stored.get(isin, "")
        item = {"isin": isin, "symbol": symbol, "in": "list" if isin in listed else "store"}
        if isin.startswith(DUMMY_PREFIX) or symbol.startswith("DUMMY"):
            explained.append({**item, "why": "demerger dummy"})
        elif isin in listed and isin not in known_isins and trust_unit(isin):
            explained.append({**item, "why": "REIT or InvIT unit HAT has no instrument for"})
        elif isin in listed and isin not in known_isins:
            wrong.append({**item, "why": "listed member with no instrument in HAT (survivorship)"})
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


def symbol_renames(ctx: Context) -> tuple[list[tuple[str, str]], int] | None:  # pragma: no cover
    """NSE's symbol changes (`symbolchange.csv`, kept under `verify_reference/nse/`) and the rows
    that could not be read; None when there is no file."""
    data = reference_file(ctx, "nse", SYMBOL_CHANGES, SYMBOL_CHANGES_URL)
    if data is None:
        return None
    renames, unread = read_symbol_changes(data)
    return [(r.old, r.new) for r in renames], unread


def capture_at(name: str) -> datetime | None:
    """`20181004105632_niftyindices.csv` -> the instant of its capture: the Wayback Machine
    stamps a capture in UTC, as `live_list_name` does."""
    stamp = name.split("_", 1)[0]
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _fetch_lists(ctx: Context, sessions: Sequence[date]) -> None:  # pragma: no cover
    """With `--fetch`: every Wayback capture of the list URLs (one per distinct content) and
    today's list from NSE's archive (`live_list_name`), kept under `verify_reference/nifty500/`."""
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
    now = datetime.now(UTC)
    last_session = list_session(sessions, now)
    kept = [capture_at(p.name) for p in ctx.reference_dir(LIST_GROUP).glob(f"*{LIVE_SUFFIX}")]
    name = live_list_name(now, ctx.end, last_session)
    if name is not None and not any(k and list_session(sessions, k) == last_session for k in kept):
        reference_file(
            ctx, LIST_GROUP, name, INDUSTRY_LIST_URL, valid=lambda data: b"ISIN" in data[:300]
        )


def live_list_name(fetched: datetime, end: date, last_session: date | None) -> str | None:
    """The name NSE's live list is kept under: the UTC instant it was fetched (`fetched`), as
    the Wayback captures are named, so `list_session` compares it with the membership of the
    last session closed when it was fetched; None, so nothing is fetched, when the run ends
    before the last session (a past window never stores today's list)."""
    if last_session is None or end < last_session:
        return None
    return f"{fetched.astimezone(UTC):%Y%m%d%H%M%S}{LIVE_SUFFIX}"


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
    findings = scope_findings(sessions, cover_start, cover_end)
    if ctx.fetch:
        _fetch_lists(ctx, sessions_all)
    exceptions = Exceptions(read_exceptions(VERIFY_EXCEPTIONS["d3"], "D3"), key_of=_item_key)
    lists = sorted(ctx.reference_dir(LIST_GROUP).glob("*.csv"))
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
        found = symbol_renames(ctx)
        renames: list[tuple[str, str]] = []
        if found is None:
            findings.append(finding("symbol_changes_missing", FAIL, [SYMBOL_CHANGES]))
        else:
            renames, unread = found
            if unread:
                findings.append({**finding("symbol_changes_unread", WARN, []), "count": unread})
        aliases = {m.instrument_id: renamed_forms(renames, {m.symbol}) for m in in_window}
        trace = trace_changes(
            in_window, notices, sessions_all, cover[0], (ctx.start, ctx.end), aliases
        )
        summary["notices"] = len(notices)
        summary["changes_traced"] = len(trace.traced)
        summary["traced_by"] = {
            "notice": sum("notice" in t for t in trace.traced),
            "isin_change": sum("isin_change" in t for t in trace.traced),
        }
        summary["traced_examples"] = trace.traced[:10]
        isins = isin_of_symbol(
            [
                *ctx.rows(
                    "SELECT symbol, isin FROM hat_instrument UNION SELECT h.symbol, i.isin "
                    "FROM hat_instrument_symbol_history h JOIN hat_instrument i "
                    "ON i.id = h.instrument_id"
                ),
                *(
                    (symbol, isin)
                    for path in lists
                    for isin, symbol in read_list(
                        path.read_text(encoding="utf-8-sig", errors="replace")
                    ).items()
                ),
            ]
        )
        unapplied = [{**i, "isin": isins.get(i["symbol"].upper())} for i in trace.unapplied]
        for code, found in (
            ("entry_without_notice", trace.entries),
            ("exit_without_notice", trace.exits),
            ("notice_not_applied", unapplied),
        ):
            if items := exceptions.split(code, found):
                findings.append(dated(finding(code, FAIL, items), [i["day"] for i in items]))
    else:
        ctx.note("D3: no --notices folder with changes_parsed.json; changes not traced")
        findings.append(finding("notices_missing", FAIL, ["changes_parsed.json"]))
    known = {isin for (isin,) in ctx.rows("SELECT isin FROM hat_instrument")}
    compared = []
    mismatched: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for path in lists:
        stamp = capture_at(path.name)
        at = None if stamp is None else list_session(sessions_all, stamp)
        if at is None or not (max(cover_start, ctx.start) <= at <= min(cover_end, ctx.end)):
            continue
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
        mismatched += [{**item, "file": path.name, "session": at.isoformat()} for item in wrong]
        differences += [{**item, "file": path.name} for item in explained]
    summary["lists_compared"] = compared
    mismatched = exceptions.split("list_mismatch", mismatched)
    if mismatched:
        findings.append(
            dated(finding("list_mismatch", FAIL, mismatched), [i["session"] for i in mismatched])
        )
    if differences:
        findings.append(finding("list_difference_explained", WARN, differences))
    if not compared:
        findings.append(finding("no_archived_list", FAIL, [str(ctx.reference_dir(LIST_GROUP))]))
    findings += exceptions.findings()
    return check_result("D3", summary, findings)


def _item_key(code: str, item: Mapping[str, Any]) -> tuple[str | None, str]:
    """A D3 item's (ISIN, day): a list item by the session its list shows."""
    return item.get("isin"), item["session"] if code == "list_mismatch" else item["day"]


def isin_of_symbol(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Each symbol's ISIN, from (symbol, ISIN) pairs, for the symbols that have had one only."""
    seen: dict[str, set[str]] = defaultdict(set)
    for symbol, isin in pairs:
        if symbol and isin:
            seen[symbol.upper()].add(isin.upper())
    return {s: next(iter(i)) for s, i in seen.items() if len(i) == 1}


def scope_findings(sessions: Sequence[date], first: date, last: date) -> list[dict[str, Any]]:
    """The window's sessions against the history's cover `first..last`: a cover starting after
    `NEEDED_FROM` fails on the sessions from it to the cover's start (`history_starts_late`); the
    sessions before the cover are listed (`sessions_before_history`, warn: no backtest reads
    members there); a session after it fails (`sessions_without_membership`, dated)."""
    out = []
    late = [d for d in sessions if NEEDED_FROM <= d < first]
    if late:
        out.append(dated(finding("history_starts_late", FAIL, _span(late)), late))
    before = [d for d in sessions if d < min(first, NEEDED_FROM)]
    if before:
        out.append(finding("sessions_before_history", WARN, _span(before), sessions=len(before)))
    after = [d for d in sessions if d > last]
    if after:
        out.append(
            dated(
                finding("sessions_without_membership", FAIL, _span(after), sessions=len(after)),
                after,
            )
        )
    return out


def _span(days: Sequence[date]) -> list[dict[str, str]]:
    return [{"from": days[0].isoformat(), "to": days[-1].isoformat()}]


__all__ = [
    "Member",
    "NoticeChange",
    "Trace",
    "compare_list",
    "notice_changes",
    "read_list",
    "run",
    "scope_findings",
    "size_runs",
    "trace_changes",
]
