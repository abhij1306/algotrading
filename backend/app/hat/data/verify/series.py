"""D7: the index and ETF series against what NSE published.

- `index_daily` against NSE's daily index files (`raw/ind_close_all_DDMMYYYY.csv`), read by a
  reader of its own: every stored level of a day with a file equals the file's open, high, low
  and close, and every wanted index the file holds is stored. A stored day without a kept file
  (filled from niftyindices or the MCP when NSE's archive had none) is compared with
  niftyindices' copy of the file (kept under `verify_reference/index/`, fetched with `--fetch`),
  else with niftyindices' price history of that index and year (`history_<index>_<year>.json`
  there: the source the history build filled those days from, NSE Indices' own series); with
  none of them it fails (unproven). Every calendar session from an index's first day
  (`INDEX_STARTS`) must hold that index: a session missing it fails, so a whole absent day is
  seen, unless the day's file shows NSE did not compute it (a close carried from the day before
  with no open, high or low: `index_not_computed`, warn). The check covers sessions from the
  first index day on.
- Exceptions: a failed index item the reviewed list names (`VERIFY_EXCEPTIONS["d7"]`: code,
  index, day, reason) warns as `excepted` (`keyed_exceptions.py`).
- `NIFTY 500 TRI` against niftyindices' answers kept under `raw_tri/`
  (`NIFTY_500_TRI_<from>_<to>.json`), read here by `read_tri_answers`, not by the importer's
  parser: every stored close in an answer's span equals the answer, and every answered day is
  stored. A span of the window no answer covers fails; the check's coverage ends at the last
  answer's day.
- The ETF store (`etf/daily.parquet`: split-adjusted, 1/10000 rupee, with each day's factor):
  each day's factor against the one NSE's unit-action circulars give (`--ca-data`
  `etf_unit_actions_*.csv`: the product of the later face-value splits' new/old), and its OHLC
  divided by that factor against the raw bar of the same symbol, which D1 holds equal to NSE's
  bhavcopy. No circular file fails.

Every failed finding is dated.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.paths import VERIFY_EXCEPTIONS

from ...money import exact
from ..audit import FAIL, WARN, check_result, finding
from ..bhavcopy import FetchResult
from ..index_history import (
    HISTORY_INDICES,
    HISTORY_URL,
    INDEX_ALIASES,
    INDEX_MIRROR_URL,
    INDEX_NAMES,
    INDEX_STARTS,
    NIFTY_500_TRI,
    TRI_RAW_DIR,
    index_archive_name,
    parse_history_json,
    tri_request,
)
from ..store import DATASET_INDEX, newest
from . import covered, dated
from .context import Context
from .keyed_exceptions import Exceptions, read_exceptions
from .references import reference_file

PRICES = ("open", "high", "low", "close")
_FILE_COLUMNS = (
    "Open Index Value",
    "High Index Value",
    "Low Index Value",
    "Closing Index Value",
)
ETF_SCALE = Decimal(10_000)  # the ETF store keeps prices as integers of 1/10000 rupee


def _number(raw: str | None) -> Decimal | None:
    return exact(raw or "", grouped=True)  # blank and `-` are no number


def read_index_file(text: str, names: Iterable[str]) -> dict[str, tuple[Decimal | None, ...]]:
    """The wanted indices' (open, high, low, close) of one NSE daily index file, by today's
    names (the archive's pre-2015 names read through `INDEX_ALIASES`)."""
    wanted = {n.upper() for n in names}
    out = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        name = row.get("Index Name", "").upper()
        name = INDEX_ALIASES.get(name, name)
        if name in wanted:
            out[name] = tuple(_number(row.get(c)) for c in _FILE_COLUMNS)
    return out


def not_computed(levels: Sequence[Decimal | None]) -> bool:
    """Whether a file row shows the index was not computed that day: a close (the day before's,
    carried) with no open, high or low."""
    return levels[3] is not None and all(v is None for v in levels[:3])


def compare_index_day(
    day: date,
    stored: Mapping[str, Sequence[Decimal]],
    source: Mapping[str, Sequence[Decimal | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A day's stored levels against its file: (mismatches, file indices not stored). A file
    value that is blank is no mismatch (NSE leaves a new index's open out on some days), and a
    file index NSE did not compute that day (`not_computed`) is not one to store."""
    wrong = []
    for name, levels in stored.items():
        if name not in source:
            continue
        for field, mine, theirs in zip(PRICES, levels, source[name], strict=True):
            if theirs is not None and Decimal(mine) != theirs:
                wrong.append(
                    {
                        "day": day.isoformat(),
                        "index": name,
                        "field": field,
                        "stored": str(mine),
                        "source": str(theirs),
                    }
                )
    missing = [
        {"day": day.isoformat(), "index": name}
        for name, levels in source.items()
        if name not in stored
        and levels[3] is not None
        and not not_computed(levels)
        and day >= INDEX_STARTS.get(name, day)
    ]
    return wrong, missing


_TRI_NAME = re.compile(r"^NIFTY_500_TRI_(\d{8})_(\d{8})\.json$")


@dataclass(frozen=True)
class TriAnswer:
    first: date
    last: date
    closes: dict[date, Decimal]
    unread: int


def read_tri_answer(name: str, data: bytes) -> TriAnswer | None:
    """One kept niftyindices TRI answer (`NIFTY_500_TRI_<from>_<to>.json`, a JSON list of rows
    with `Date` `31 Dec 2012` and `TotalReturnsIndex`): its span from the name, its closes, and
    the rows that could not be read. None for another name or no JSON list."""
    match = _TRI_NAME.match(name)
    try:
        rows = json.loads(data.decode("utf-8-sig", "replace"))
    except ValueError:
        return None
    if match is None or not isinstance(rows, list):
        return None
    first, last = (datetime.strptime(g, "%Y%m%d").date() for g in match.groups())  # noqa: DTZ007
    closes, unread = {}, 0
    for row in rows:
        try:
            day = datetime.strptime(str(row["Date"]).strip(), "%d %b %Y").date()  # noqa: DTZ007
        except (KeyError, TypeError, ValueError):
            unread += 1
            continue
        value = exact(str(row.get("TotalReturnsIndex", "")), grouped=True)
        if value is None or value <= 0:
            unread += 1
            continue
        closes[day] = value
    return TriAnswer(first, last, closes, unread)


def compare_tri(
    stored: Mapping[date, Decimal],
    answers: Sequence[TriAnswer],
    sessions: Sequence[date],
    start: date,
    end: date,
) -> dict[str, list[Any]]:
    """The stored TRI closes of `start..end` against the answers (a later answer's day replaces
    an earlier one's): `tri_mismatch`, `tri_not_in_niftyindices` (a stored day inside an
    answer's span that the answer lacks), `tri_not_stored` (an answered day not stored, or a
    session from the TRI's first day with no close) and `tri_unanswered` (the sessions no
    answer's span covers)."""
    answered: dict[date, Decimal] = {}
    spans = []
    for answer in sorted(answers, key=lambda a: (a.first, a.last)):
        answered.update(answer.closes)
        spans.append((answer.first, answer.last))
    first = INDEX_STARTS[NIFTY_500_TRI]

    def in_span(day: date) -> bool:
        return any(a <= day <= b for a, b in spans)

    out: dict[str, list[Any]] = defaultdict(list)
    days = sorted({d for d in (*stored, *answered, *sessions) if start <= d <= end})
    session_set = set(sessions)
    for day in days:
        mine, theirs = stored.get(day), answered.get(day)
        if mine is not None and theirs is not None:
            if Decimal(mine) != theirs:
                out["tri_mismatch"].append([day.isoformat(), str(mine), str(theirs)])
        elif mine is not None and in_span(day):
            out["tri_not_in_niftyindices"].append(day.isoformat())
        elif theirs is not None or (day in session_set and day >= first and in_span(day)):
            out["tri_not_stored"].append(day.isoformat())
        if day in session_set and day >= first and not in_span(day):
            out["tri_unanswered"].append(day.isoformat())
    return out


def read_unit_actions(text: str) -> tuple[dict[str, list[tuple[date, Decimal]]], int]:
    """NSE's ETF unit actions (`etf_unit_actions_*.csv`: symbol, old and new face value, the
    ex date `19-Dec-2019`), read here: symbol -> [(ex date, new/old)], and the rows not read."""
    out: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    unread = 0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            day = datetime.strptime(row["ex_effective_date"].strip(), "%d-%b-%Y").date()  # noqa: DTZ007
        except (KeyError, AttributeError, ValueError):
            unread += 1
            continue
        new, old = (
            exact(row.get("new_face_value_rs") or ""),
            exact(row.get("old_face_value_rs") or ""),
        )
        if new is None or old is None or new <= 0 or old <= 0:
            unread += 1
            continue
        out[row["symbol"].strip().upper()].append((day, new / old))
    return out, unread


def unit_factor(actions: Sequence[tuple[date, Decimal]], day: date) -> Decimal:
    """The split adjustment of `day`'s prices to today's units: the product of the later unit
    actions' new/old face value (1 when none is later)."""
    factor = Decimal(1)
    for ex_date, ratio in actions:
        if ex_date > day:
            factor *= ratio
    return factor


def compare_etf(
    rows: Iterable[Mapping[str, Any]],
    raw: Mapping[tuple[str, date], Sequence[Decimal]],
    actions: Mapping[str, Sequence[tuple[date, Decimal]]],
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The ETF store's rows (`symbol`, `date`, the prices in 1/10000 rupee, `factor`) against
    NSE's unit actions and the raw bars by (symbol, date): (compared, price mismatches, rows
    with no raw bar, factor mismatches). The factor must equal `unit_factor` of the symbol's
    actions; a price matches when its value divided by that factor is within the store's
    rounding (half of 1/10000 rupee, divided by the factor) of the raw one."""
    compared, wrong, missing, factors = 0, [], [], []
    for row in rows:
        key = (str(row["symbol"]), row["date"])
        factor = unit_factor(actions.get(key[0], ()), key[1])
        stored_factor = exact(str(row["factor"]))
        if stored_factor != factor:
            factors.append(
                {
                    "symbol": key[0],
                    "day": key[1].isoformat(),
                    "stored": str(row["factor"]),
                    "nse": str(factor),
                }
            )
        bar = raw.get(key)
        if bar is None:
            missing.append({"symbol": key[0], "day": key[1].isoformat()})
            continue
        compared += 1
        slack = Decimal("0.00005") / factor + Decimal("0.00005")
        for field, mine in zip(PRICES, bar, strict=True):
            unadjusted = Decimal(int(row[field])) / ETF_SCALE / factor
            if abs(unadjusted - Decimal(mine)) > slack:
                wrong.append(
                    {
                        "symbol": key[0],
                        "day": key[1].isoformat(),
                        "field": field,
                        "etf_store_unadjusted": str(unadjusted.quantize(Decimal("0.0001"))),
                        "raw": str(mine),
                    }
                )
    return compared, wrong, missing, factors


# The latest ingest run's bar per (symbol, date) of the few ETF symbols, filtered before it is
# ranked (the `_latest` views rank the whole store).
_RAW_ETF_SQL = f"""
SELECT r.symbol, r.date, {newest("open")}, {newest("high")}, {newest("low")}, {newest("close")}
FROM bars_daily_raw r
WHERE r.symbol IN (SELECT unnest(CAST(? AS VARCHAR[]))) AND r.date BETWEEN ? AND ?
GROUP BY ALL
"""  # noqa: S608 - `newest` is a constant expression


def _index_file(ctx: Context, day: date) -> bytes | None:  # pragma: no cover - files
    """NSE's daily index file of `day`: the kept archive file, else niftyindices' copy of it
    kept (or, with `--fetch`, downloaded) under `verify_reference/index/`. A copy niftyindices
    lacks comes back as an HTML page, which holds no index row and so reads as no file."""
    name = index_archive_name(day)
    if ctx.store.raw_exists(name):
        return ctx.store.read_raw(name)
    return reference_file(ctx, "index", name, INDEX_MIRROR_URL + name, valid=_index_csv)


def _index_csv(data: bytes) -> bool:
    return b"Index Name" in data[:200]


def _history(
    ctx: Context, name: str, year: int
) -> dict[date, tuple[Decimal, ...]]:  # pragma: no cover
    """niftyindices' price history of `name` for `year` (kept, or with `--fetch` asked once
    and kept, as `verify_reference/index/history_<NAME>_<year>.json`): day -> (open, high, low,
    close); empty for an index it does not serve or no answer."""
    base = HISTORY_INDICES.get(name)
    if base is None:
        return {}

    def fetch(_url: str) -> FetchResult:
        from ..index_history import http_poster  # noqa: PLC0415 - the network, only when fetching

        return http_poster()(HISTORY_URL, tri_request(base, date(year, 1, 1), date(year, 12, 31)))

    data = reference_file(
        ctx,
        "index",
        f"history_{name.replace(' ', '_')}_{year}.json",
        HISTORY_URL,
        fetch=fetch,
        valid=lambda d: bool(parse_history_json(d, name)),
    )
    points = (parse_history_json(data, name) or []) if data is not None else []
    return {p.day: (p.row.open, p.row.high, p.row.low, p.row.close) for p in points}


def _tri_answers(folder: Path) -> list[TriAnswer]:  # pragma: no cover - files
    found = (read_tri_answer(p.name, p.read_bytes()) for p in sorted(folder.glob("*.json")))
    return [a for a in found if a is not None]


def _dated(code: str, items: Sequence[Any], days: Iterable[str | date]) -> dict[str, Any]:
    return dated(finding(code, FAIL, items), days)


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    names = INDEX_NAMES  # the TRI is not in NSE's daily file: niftyindices' answers check it
    sessions = [
        d
        for (d,) in ctx.rows(
            "SELECT date FROM hat_trading_calendar WHERE is_trading AND date BETWEEN :a AND :b "
            "ORDER BY 1",
            a=ctx.start,
            b=ctx.end,
        )
    ]
    stored_days = {d for d in ctx.store.days(DATASET_INDEX) if ctx.start <= d <= ctx.end}
    wrong: list[dict[str, Any]] = []
    unstored: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    uncomputed: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    compared = Counter[str]()
    by_history = Counter[str]()
    histories: dict[tuple[str, int], dict[date, tuple[Decimal, ...]]] = {}
    tri_stored: dict[date, Decimal] = {}
    for day in sessions:
        rows = ctx.store.read_index(day) if day in stored_days else []
        tri_stored.update((day, Decimal(r.close)) for r in rows if r.index_name == NIFTY_500_TRI)
        stored = {
            r.index_name: (r.open, r.high, r.low, r.close) for r in rows if r.index_name in names
        }
        lacking = [n for n in names if n not in stored and day >= INDEX_STARTS[n]]
        data = _index_file(ctx, day)
        source = {} if data is None else read_index_file(data.decode("utf-8-sig", "replace"), names)
        for n in lacking:
            kind = uncomputed if n in source and not_computed(source[n]) else absent
            kind.append({"day": day.isoformat(), "index": n})
        if not stored:
            continue
        bad, missing = compare_index_day(day, stored, source)
        wrong += bad
        unstored += missing
        for index in stored:
            if index in source:
                compared[index] += 1
                continue
            if (index, day.year) not in histories:
                histories[index, day.year] = _history(ctx, index, day.year)
            history = histories[index, day.year]
            if day not in history:
                unproven.append({"day": day.isoformat(), "index": index})
                continue
            bad, _ = compare_index_day(day, {index: stored[index]}, {index: history[day]})
            wrong += [{**b, "reference": "niftyindices history"} for b in bad]
            by_history[index] += 1
    findings = []
    exceptions = Exceptions.of(
        read_exceptions(VERIFY_EXCEPTIONS["d7"]), lambda _code, i: (i["index"], i["day"])
    )
    for code, found in (
        ("index_level_mismatch", wrong),
        ("index_in_file_not_stored", unstored),
        ("index_session_not_stored", absent),
        ("index_day_without_nse_file", unproven),
    ):
        if items := exceptions.split(code, found):
            findings.append(_dated(code, items, [i["day"] for i in items]))
    if uncomputed:
        findings.append(finding("index_not_computed", WARN, uncomputed))
    findings += exceptions.findings()
    answers = _tri_answers(ctx.root / TRI_RAW_DIR)
    tri = compare_tri(tri_stored, answers, sessions, ctx.start, ctx.end)
    last_answer = max((a.last for a in answers), default=None)
    for code in ("tri_mismatch", "tri_not_in_niftyindices", "tri_not_stored"):
        if tri[code]:
            days = [i[0] if isinstance(i, list) else i for i in tri[code]]
            findings.append(_dated(code, tri[code], days))
    gaps = [d for d in tri["tri_unanswered"] if last_answer is None or d <= last_answer.isoformat()]
    if gaps:  # a span inside the answers' reach that no answer covers; after it: not covered
        findings.append(_dated("tri_unanswered", gaps, gaps))
    unread_tri = sum(a.unread for a in answers)
    if unread_tri:
        findings.append({**finding("tri_rows_unread", WARN, []), "count": unread_tri})
    etf = _etf(ctx, findings)
    summary = {
        "sessions": len(sessions),
        "index_days_compared": dict(compared),
        "index_days_compared_to_history": dict(by_history),
        "tri_answers": len(answers),
        "tri_compared": len(set(tri_stored) & {d for a in answers for d in a.closes}),
        "tri_last_answer": None if last_answer is None else last_answer.isoformat(),
        "etf": etf,
    }
    result = check_result("D7", summary, findings)
    first = max(ctx.start, min(INDEX_STARTS.values()))
    last = ctx.end if last_answer is None else min(ctx.end, last_answer)
    return covered(result, first, last)


def _etf(ctx: Context, findings: list[dict[str, Any]]) -> dict[str, Any]:  # pragma: no cover
    import polars as pl  # noqa: PLC0415

    etf_path = ctx.root / "etf" / "daily.parquet"
    if not etf_path.is_file():
        findings.append(finding("etf_store_missing", FAIL, ["etf/daily.parquet"]))
        return {"store": None}
    circulars = sorted(ctx.ca_data.glob("etf_unit_actions_*.csv")) if ctx.ca_data else []
    if not circulars:
        findings.append(finding("etf_unit_actions_missing", FAIL, ["etf_unit_actions_*.csv"]))
    actions: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    unread = 0
    for path in circulars:
        found, n = read_unit_actions(path.read_text(encoding="utf-8-sig"))
        unread += n
        for symbol, items in found.items():
            actions[symbol] += items
    if unread:
        findings.append({**finding("etf_unit_actions_unread", WARN, []), "count": unread})
    rows = pl.read_parquet(etf_path).filter(pl.col("date").is_between(ctx.start, ctx.end))
    symbols = sorted(set(rows["symbol"].to_list()))
    with ctx.store.connect() as con:
        raw = {
            (s, d): (o, h, low, c)
            for s, d, o, h, low, c in con.execute(
                _RAW_ETF_SQL, [symbols, ctx.start, ctx.end]
            ).fetchall()
        }
    n, bad, no_bar, factors = compare_etf(rows.iter_rows(named=True), raw, actions)
    for code, items in (
        ("etf_factor_not_nse", factors),
        ("etf_price_mismatch", bad),
        ("etf_day_without_raw_bar", no_bar),
    ):
        if items and circulars:
            findings.append(_dated(code, items, [i["day"] for i in items]))
    return {
        "store": "etf/daily.parquet",
        "rows": rows.height,
        "compared": n,
        "unit_actions": {s: [(d.isoformat(), str(r)) for d, r in a] for s, a in actions.items()},
    }
