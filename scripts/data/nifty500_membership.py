"""Build the point-in-time NIFTY 500 membership CSV that `admin load-index-membership` loads.

NSE publishes only today's list, so the history is rebuilt by undoing each change notice from
today's list back, newest first, with the repo's strict `index_membership.undo_changes`.

Usage (from the repo root, backend venv active):

    python scripts/data/nifty500_membership.py --work DIR fetch       # notices + today's list
    python scripts/data/nifty500_membership.py --work DIR text        # PDF -> text
    python scripts/data/nifty500_membership.py --work DIR parse       # NIFTY 500 sections
    python scripts/data/nifty500_membership.py --work DIR reference   # hat_live + bars, read-only
    python scripts/data/nifty500_membership.py --work DIR build --as-of 2026-10-25

The steps, each writing under `--work` (or `NIFTY500_DIR`), a directory outside the repo:

- `fetch`: the niftyindices press-release page -> `press_index.csv` (date, href, title); every
  notice from 2011 on that is not a debt, ETF or launch notice -> `raw/press/*.pdf` (skips files
  it has); today's list -> `raw/ind_nifty500list.csv`.
- `text`: each PDF's text -> `raw/text/*.txt` (pdfplumber; skips files it has).
- `parse`: from each text naming NIFTY 500 or CNX 500, its effective dates and the excluded and
  included symbols of its NIFTY 500 sections -> `changes_parsed.json`.
- `reference`: from the database, instruments, symbol history and ISIN lineage ->
  `ref/hat_live_reference.json`; from `HAT_DATA_ROOT/bars_daily_raw`, each (instrument, ISIN,
  symbol)'s first and last bar day -> `ref/bar_symbol_span.parquet`.
- `build`: resolves each notice symbol to an instrument (symbol history and bar spans, the
  nearest within 400 days of the effective day), keyed by the end of its ISIN lineage chain,
  undoes the changes from today's list, splits each interval per ISIN at the lineage dates and
  writes `nifty500-membership.csv` (`index_membership.COLUMNS`). It refuses to write when a
  symbol does not resolve or a change does not fit the list.

Two hand-kept inputs in `--work` correct what the parser cannot read, each entry taken from the
notices: `overrides.json` (per notice file: `date` for an effective date the parser missed,
`skip`, `changes` replacing its parsed sections, `drop` / `drop_from` for revised notices; and
`_extra`, changes from notices not on the press page) and `symbol_alias.json` (a notice symbol ->
another symbol, `ISIN:<isin>` or `PSEUDO:<isin>` for a member HAT has no instrument for, which
the CSV leaves out). Write the coverage report beside the CSV; runbook "Index membership".
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SITE = "https://www.niftyindices.com"
PRESS_PAGE = f"{SITE}/press-release"
LIST_URL = f"{SITE}/IndexConstituent/ind_nifty500list.csv"
LIST_SOURCE = "ind_nifty500list.csv"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130"
FIRST_YEAR = 2011
SKIP_TITLES = (
    "fixed income",
    "g-sec",
    "gsec",
    "launch",
    "etf",
    "bharat bond",
    "sdl",
    "t-bill",
    "money market",
    "bond",
    "debt",
    "tracking error",
    "webinar",
    "mou",
    "agreement",
    "hong kong",
    "korea",
    "iosco",
)
RESOLVE_DAYS = 400
OPEN_END = date(2100, 1, 1)

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(
    rf"({MONTHS})\s+(\d{{1,2}}),?\s*(\d{{4}})|(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS}),?\s*(\d{{4}})",
    re.IGNORECASE,
)
EFFECTIVE_RE = re.compile(
    r"(?:effective\s+(?:from|on)|w\.?\s?e\.?\s?f\.?)\s*:?\s*(?:the\s+)?(?:close\s+of\s+)?"
    rf"((?:{MONTHS})\s+\d{{1,2}},?\s*\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS}),?\s*\d{{4}})",
    re.IGNORECASE | re.DOTALL,
)
HEAD_500 = re.compile(
    r"^\s*(?:\(?(?:\d{1,2}|[A-Za-z]|[ivxIVX]{1,4})[\).]\s*)?(?:S&P\s+)?(?:CNX|NIFTY)\s?500"
    r"(?:\s+Index)?\s*:?\s*$",
    re.IGNORECASE,
)
HEAD_ANY = re.compile(r"^\s*\(?(?:\d{1,2}|[A-Za-z]|[ivxIVX]{1,4})[\).]\s+\S")
ITEM = re.compile(
    r"^\s*(\d{1,3})[\.\)]?\s+(?:(.+?)\s+)?((?=[A-Z0-9&\-_]*[A-Z])[A-Z0-9][A-Z0-9&\-_]{1,19})"
    r"\s*[\*#]*\s*$"
)
MENTIONS_500 = re.compile(r"(?:NIFTY|CNX)\s?500", re.IGNORECASE)
PRESS_ITEM = re.compile(
    r"data-date=\"([^\"]+)\"[^>]*>\s*<p>[^<]*</p>\s*<a href='([^']+)'[^>]*>([^<]+)</a>"
)

Event = tuple[date, str, list[str], list[str]]  # day, notice file, excluded, included


# ---------------------------------------------------------------------------------------------
# notices


def press_items(page: str) -> list[tuple[str, str, str]]:
    """(date, href, title) of each notice on the press-release page."""
    return PRESS_ITEM.findall(page)


def wanted_notice(day: str, title: str) -> bool:
    """A notice from FIRST_YEAR on that may change an equity index."""
    if datetime.strptime(day, "%b %d, %Y").year < FIRST_YEAR:
        return False
    return not any(s in title.lower() for s in SKIP_TITLES)


def parse_date(text: str) -> str | None:
    """The first "Month D, YYYY" or "Dth Month YYYY" date in text, ISO."""
    text = re.sub(r"\s+", " ", text.replace(",", ", ")).replace(" ,", ",")
    m = DATE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        month, day, year = m.group(1), m.group(2), m.group(3)
    else:
        day, month, year = m.group(4), m.group(5), m.group(6)
    return datetime.strptime(f"{month.title()} {int(day)} {year}", "%B %d %Y").date().isoformat()


def parse_notice(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """(effective dates, NIFTY 500 sections) of a notice's text. A section lists `excluded` and
    `included` [name, symbol] pairs from the numbered rows under its heading, until the next
    heading."""
    lines = text.replace("\f", "\n").split("\n")
    effective = sorted({parse_date(m.group(1)) for m in EFFECTIVE_RE.finditer(text)} - {None})
    sections = []
    i = 0
    while i < len(lines):
        if not HEAD_500.match(lines[i]):
            i += 1
            continue
        sec: dict[str, Any] = {"header": lines[i].strip(), "excluded": [], "included": []}
        mode = None
        j = i + 1
        while j < len(lines):
            line = lines[j]
            item = ITEM.match(line)
            if (HEAD_ANY.match(line) and not item) or re.match(
                r"^\s*(About |[A-Z]\.\s|[A-Z]\)\s)", line
            ):
                break
            low = line.lower()
            if not item:
                if "excluded" in low or "exclusion" in low:
                    mode = "excluded"
                elif "included" in low or "inclusion" in low:
                    mode = "included"
            elif mode and not low.startswith("sr"):
                sec[mode].append([(item.group(2) or "").strip(), item.group(3).strip()])
            j += 1
        sections.append(sec)
        i = j
    return effective, sections


def notice_events(
    parsed: Iterable[Mapping[str, Any]], overrides: Mapping[str, Any], start: date, as_of: date
) -> list[Event]:
    """The changes in (start, as_of], from the parsed notices corrected by `overrides`."""
    events: list[Event] = []
    for notice in parsed:
        f = notice["file"]
        ov = overrides.get(f, {})
        if not isinstance(ov, dict) or ov.get("skip"):
            continue
        if "changes" in ov:
            events += [
                (date.fromisoformat(c["date"]), f, c.get("excluded", []), c.get("included", []))
                for c in ov["changes"]
            ]
            continue
        sections = [s for s in notice["sections"] if s["excluded"] or s["included"]]
        if not sections:
            continue
        day = ov.get("date") or (notice["effective"][0] if len(notice["effective"]) == 1 else None)
        if day is None:
            print(f"NO DATE {f} {notice['effective']}: skipped; add its date to overrides.json")
            continue
        events += [
            (
                date.fromisoformat(day),
                f,
                [x[1] for x in s["excluded"]],
                [x[1] for x in s["included"]],
            )
            for s in sections
        ]
    drops: dict[str, set[str]] = defaultdict(set)
    for f, ov in overrides.items():
        if not isinstance(ov, dict):
            continue
        for target, symbols in ov.get("drop", {}).items():
            drops[target].update(symbols)
        if ov.get("drop_from"):
            drops[ov["drop_from"]].update(x for e in events if e[1] == f for x in e[2] + e[3])
    events = [
        (d, f, [x for x in exc if x not in drops[f]], [x for x in inc if x not in drops[f]])
        for d, f, exc, inc in events
    ]
    events += [
        (date.fromisoformat(c["date"]), c["file"], c.get("excluded", []), c.get("included", []))
        for c in overrides.get("_extra", [])
    ]
    return [e for e in events if start < e[0] <= as_of]


# ---------------------------------------------------------------------------------------------
# instruments


@dataclass
class Reference:
    """The instruments a notice symbol can resolve to, from hat_live and the bar spans."""

    isin: dict[str, str]  # instrument id -> ISIN
    lineage: list[tuple[str, str, str]]  # linked (old id, new id, effective day)
    spans: dict[str, list[tuple[date, date, str]]]  # symbol -> (first, last, instrument id)
    history: dict[str, list[tuple[date, date | None, str]]]  # instrument id -> symbol spans
    alias: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_isin = {v: k for k, v in self.isin.items()}
        self.successor = {old: new for old, new, _ in self.lineage}
        self.predecessors: dict[str, list[tuple[str, date]]] = defaultdict(list)
        for old, new, eff in self.lineage:
            self.predecessors[new].append((old, date.fromisoformat(eff)))

    def chain_end(self, iid: str) -> str:
        seen = set()
        while iid in self.successor and iid not in seen:
            seen.add(iid)
            iid = self.successor[iid]
        return iid

    def key(self, iid: str) -> str:
        """The membership key: the newest instrument of the ISIN chain."""
        return iid if iid.startswith("PSEUDO:") else self.chain_end(iid)

    def resolve(self, symbol: str, day: date, side: str) -> str | None:
        """The instrument `symbol` named around `day`: on the day before for an exclusion
        (`out`), on the day for an inclusion (`in`), else the nearest span within RESOLVE_DAYS."""
        symbol = self.alias.get(symbol, symbol)
        if symbol.startswith("ISIN:"):
            return self.by_isin.get(symbol[5:])
        if symbol.startswith("PSEUDO:"):
            return symbol
        ref = day - timedelta(days=1) if side == "out" else day

        def distance(span: tuple[date, date, str]) -> int:
            first, last, _ = span
            if first <= ref <= last:
                return 0
            return min(abs((first - ref).days), abs((last - ref).days))

        candidates = self.spans.get(symbol)
        if not candidates:
            return None
        best = min(candidates, key=distance)
        return best[2] if distance(best) <= RESOLVE_DAYS else None

    def symbol_on(self, iid: str, day: date, fallback: str) -> str:
        for first, last, symbol in self.history.get(iid, ()):
            if first <= day and (last is None or last > day):
                return symbol
        return fallback

    def chain(self, key: str) -> list[tuple[str, date | None]]:
        """[(instrument, valid from)] of a key's ISIN chain, oldest first (None for the oldest);
        of several predecessors, the latest."""
        out: list[tuple[str, date | None]] = [(key, None)]
        while self.predecessors.get(out[0][0]):
            old, eff = max(self.predecessors[out[0][0]], key=lambda p: p[1])
            out[0] = (out[0][0], eff)
            out.insert(0, (old, None))
        return out


def split_by_isin(
    ref: Reference, key: str, start: date, end: date | None
) -> list[tuple[str, date, date | None]]:
    """A key's membership [start, end) as (instrument, from, to) per ISIN of its chain."""
    chain = ref.chain(key)
    out = []
    for n, (iid, valid_from) in enumerate(chain):
        valid_to = chain[n + 1][1] if n + 1 < len(chain) else None
        a = max(start, valid_from) if valid_from else start
        b = end if valid_to is None else (min(end, valid_to) if end else valid_to)
        if b is None or b > a:
            out.append((iid, a, b))
    return out


def load_reference(work: Path) -> Reference:
    import polars as pl  # noqa: PLC0415

    raw = json.loads((work / "ref" / "hat_live_reference.json").read_text())
    spans: dict[str, list[tuple[date, date, str]]] = defaultdict(list)
    for iid, _isin, symbol, first, last, _n in pl.read_parquet(
        work / "ref" / "bar_symbol_span.parquet"
    ).iter_rows():
        spans[symbol].append((first, last, iid))
    history: dict[str, list[tuple[date, date | None, str]]] = defaultdict(list)
    for iid, symbol, valid_from, valid_to in raw["symbol_history"]:
        first = date.fromisoformat(valid_from)
        last = date.fromisoformat(valid_to) if valid_to else None
        spans[symbol].append((first, last or OPEN_END, iid))
        history[iid].append((first, last, symbol))
    alias_file = work / "symbol_alias.json"
    return Reference(
        isin={r[0]: r[1] for r in raw["instruments"]},
        lineage=[(o, n, e) for o, n, e, status, _ in raw["lineage"] if status == "linked"],
        spans=dict(spans),
        history=dict(history),
        alias=json.loads(alias_file.read_text()) if alias_file.exists() else {},
    )


# ---------------------------------------------------------------------------------------------
# commands


def _get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https checked
        return resp.read()


def cmd_fetch(work: Path) -> None:
    items = press_items(_get(PRESS_PAGE).decode("utf-8", "ignore"))
    with (work / "press_index.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "href", "title"])
        w.writerows(items)
    (work / "raw").mkdir(exist_ok=True)
    (work / "raw" / LIST_SOURCE).write_bytes(_get(LIST_URL))
    press = work / "raw" / "press"
    press.mkdir(parents=True, exist_ok=True)
    got, failed = 0, []
    for day, href, title in items:
        target = press / os.path.basename(href)
        if not wanted_notice(day, title) or (target.exists() and target.stat().st_size):
            continue
        try:
            tmp = target.with_suffix(".part")  # a stopped run leaves no partial PDF
            tmp.write_bytes(_get(SITE + href))
            os.replace(tmp, target)
            got += 1
        except OSError as e:
            failed.append(f"{href}: {e}")
        time.sleep(0.3)
    print(f"{len(items)} notices listed, {got} downloaded, {len(failed)} failed")
    for line in failed:
        print(" ", line)


def cmd_text(work: Path) -> None:
    import pdfplumber  # noqa: PLC0415 - only this step reads PDFs

    out = work / "raw" / "text"
    out.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((work / "raw" / "press").glob("*.pdf")):
        target = out / f"{pdf.stem}.txt"
        if target.exists():
            continue
        try:
            with pdfplumber.open(pdf) as doc:
                text = "\n\f".join(p.dedupe_chars().extract_text() or "" for p in doc.pages)
        except Exception as e:  # noqa: BLE001 - a broken file is recorded, not fatal
            text = f"EXTRACT_ERROR {e}"
        target.write_text(text)


def cmd_parse(work: Path) -> None:
    out = []
    for path in sorted((work / "raw" / "text").glob("*.txt")):
        text = path.read_text()
        if not MENTIONS_500.search(text):
            continue
        effective, sections = parse_notice(text)
        out.append({"file": f"{path.stem}.pdf", "effective": effective, "sections": sections})
        changes = sum(len(s["excluded"]) + len(s["included"]) for s in sections)
        if sections and (len(effective) != 1 or not changes):
            print(f"CHECK {path.stem}.pdf effective {effective} changes {changes}")
    (work / "changes_parsed.json").write_text(json.dumps(out, indent=1))
    print(
        f"{len(out)} notices name NIFTY 500; {sum(1 for o in out if o['sections'])} with sections"
    )


def cmd_reference(work: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import duckdb  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415 - hat_live, after the sys.path change
    from app.paths import HAT_DATA_ROOT  # noqa: PLC0415

    def rows(s: Any, sql: str) -> list[list[Any]]:  # noqa: ANN401
        return [
            [v if v is None or isinstance(v, int | float | str | bool) else str(v) for v in r]
            for r in s.execute(text(sql))
        ]

    with engine.connect() as s:
        s.execute(text("set transaction read only"))
        out = {
            "instruments": rows(s, "select id, isin, symbol from hat_instrument"),
            "symbol_history": rows(
                s,
                "select instrument_id, symbol, valid_from, valid_to"
                " from hat_instrument_symbol_history",
            ),
            "lineage": rows(
                s,
                "select old_instrument_id, new_instrument_id, effective_date, status, reason"
                " from hat_isin_lineage",
            ),
        }
        s.rollback()
    (work / "ref").mkdir(exist_ok=True)
    (work / "ref" / "hat_live_reference.json").write_text(json.dumps(out))
    bars = (HAT_DATA_ROOT / "bars_daily_raw" / "*" / "*.parquet").as_posix()
    con = duckdb.connect(config={"memory_limit": "1GB"})
    spans = con.execute(
        "select instrument_id, isin, symbol, min(date) d0, max(date) d1, count(distinct date) n"
        " from read_parquet(?, hive_partitioning=true) group by all",
        [bars],
    ).pl()
    spans.write_parquet(work / "ref" / "bar_symbol_span.parquet")
    print({k: len(v) for k, v in out.items()}, "bar spans from", HAT_DATA_ROOT)


def current_list(path: Path, ref: Reference) -> dict[str, str]:
    """Today's members, key -> symbol; an ISIN without an instrument becomes `PSEUDO:<isin>`."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        isin = r["ISIN Code"].strip()
        out[ref.key(ref.by_isin.get(isin) or f"PSEUDO:{isin}")] = r["Symbol"].strip()
    return out


def cmd_build(work: Path, start: date, as_of: date, out: Path) -> int:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.hat.data.index_membership import (  # noqa: PLC0415 - after the sys.path change
        COLUMNS,
        NIFTY_500,
        Change,
        undo_changes,
    )

    ref = load_reference(work)
    overrides_file = work / "overrides.json"
    overrides = json.loads(overrides_file.read_text()) if overrides_file.exists() else {}
    parsed = json.loads((work / "changes_parsed.json").read_text())
    current = current_list(work / "raw" / LIST_SOURCE, ref)
    changes, unresolved = [], []
    for day, notice, excluded, included in notice_events(parsed, overrides, start, as_of):
        sides: dict[str, dict[str, str]] = {"in": {}, "out": {}}
        for side, symbols in (("in", included), ("out", excluded)):
            for symbol in symbols:
                iid = ref.resolve(symbol, day, side)
                if iid is None:
                    unresolved.append(f"{day} {notice} {side} {symbol}")
                else:
                    sides[side][ref.key(iid)] = symbol
        for key in sides["in"].keys() & sides["out"].keys():
            del sides["in"][key], sides["out"][key]
        changes.append(Change(day, sides["in"], sides["out"], notice))
    if unresolved:
        print("UNRESOLVED (add to symbol_alias.json):", *unresolved, sep="\n  ")
        return 1
    members = undo_changes(current, as_of, changes, start, LIST_SOURCE)
    rows, pseudo = [], []
    for m in members:
        key = str(m.key)
        if key.startswith("PSEUDO:"):
            pseudo.append(f"{m.symbol} {m.effective_from}..{m.effective_to or ''}")
            continue
        for iid, a, b in split_by_isin(ref, key, m.effective_from, m.effective_to):
            rows.append(
                (
                    NIFTY_500,
                    ref.isin[iid],
                    ref.symbol_on(iid, a, m.symbol),
                    a.isoformat(),
                    b.isoformat() if b else "",
                    m.source,
                )
            )
    rows.sort(key=lambda r: (r[3], r[1]))
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(rows)
    print(f"{len(changes)} changes, {len(members)} members, {len(rows)} rows -> {out}")
    if pseudo:
        print("left out (no instrument):", "; ".join(pseudo))
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="nifty500_membership.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--work",
        type=Path,
        default=os.environ.get("NIFTY500_DIR") or None,
        help="the working directory (default: $NIFTY500_DIR)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "text", "parse", "reference"):
        sub.add_parser(name)
    b = sub.add_parser("build")
    b.add_argument("--as-of", type=date.fromisoformat, required=True, help="today's list's day")
    b.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    b.add_argument("--out", type=Path, help="default: <work>/nifty500-membership.csv")
    args = p.parse_args(argv)
    if args.work is None:
        p.error("--work or NIFTY500_DIR is required")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    match args.cmd:
        case "fetch":
            cmd_fetch(work)
        case "text":
            cmd_text(work)
        case "parse":
            cmd_parse(work)
        case "reference":
            cmd_reference(work)
        case "build":
            return cmd_build(
                work, args.start, args.as_of, args.out or work / "nifty500-membership.csv"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
