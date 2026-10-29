"""D7: the index and ETF series against what NSE published.

- `index_daily` against NSE's daily index files (`raw/ind_close_all_DDMMYYYY.csv`), read by a
  reader of its own: every stored level of a day with a file equals the file's open, high, low
  and close, and every wanted index the file holds is stored. A stored day without a kept file
  (filled from niftyindices or the MCP when NSE's archive had none) is compared with
  niftyindices' copy of the file (kept under `raw/reference/index/`, fetched with `--fetch`), or
  counted when there is none.
- `NIFTY 500 TRI` against niftyindices' answers kept under `raw_tri/` (`index_history.verify_tri`).
- The ETF store (`etf/daily.parquet`: split-adjusted, 1/10000 rupee, with each day's factor):
  each day's OHLC divided by its factor against the raw bar of the same symbol, which D1 holds
  equal to NSE's bhavcopy.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from ...money import exact, require_exact
from ..audit import FAIL, WARN, check_result, finding
from ..index_history import (
    INDEX_ALIASES,
    INDEX_MIRROR_URL,
    INDEX_NAMES,
    INDEX_STARTS,
    TRI_RAW_DIR,
    index_archive_name,
    verify_tri,
)
from ..store import DATASET_INDEX, newest
from .context import Context
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


def compare_index_day(
    day: date,
    stored: Mapping[str, Sequence[Decimal]],
    source: Mapping[str, Sequence[Decimal | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A day's stored levels against its file: (mismatches, file indices not stored). A file
    value that is blank is no mismatch (NSE leaves a new index's open out on some days)."""
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
        if name not in stored and levels[3] is not None and day >= INDEX_STARTS.get(name, day)
    ]
    return wrong, missing


def compare_etf(
    rows: Iterable[Mapping[str, Any]], raw: Mapping[tuple[str, date], Sequence[Decimal]]
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """The ETF store's rows (`symbol`, `date`, the prices in 1/10000 rupee, `factor`) against
    the raw bars by (symbol, date): (compared, mismatches, rows with no raw bar). A price
    matches when its unadjusted value is within the store's rounding (half of 1/10000 rupee,
    divided by the factor) of the raw one."""
    compared, wrong, missing = 0, [], []
    for row in rows:
        key = (str(row["symbol"]), row["date"])
        bar = raw.get(key)
        if bar is None:
            missing.append({"symbol": key[0], "day": key[1].isoformat()})
            continue
        compared += 1
        factor = require_exact(row["factor"], "etf_factor", floats=True)
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
    return compared, wrong, missing


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
    kept (or, with `--fetch`, downloaded) under `raw/reference/index/`. A copy niftyindices
    lacks comes back as an HTML page, which holds no index row and so reads as no file."""
    name = index_archive_name(day)
    if ctx.store.raw_exists(name):
        return ctx.store.read_raw(name)
    return reference_file(ctx, "index", name, INDEX_MIRROR_URL + name, valid=_index_csv)


def _index_csv(data: bytes) -> bool:
    return b"Index Name" in data[:200]


def _last_tri_day(ctx: Context) -> date | None:  # pragma: no cover - files
    """The last day the kept TRI answers reach (their names end with it)."""
    ends = [
        date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
        for p in (ctx.root / TRI_RAW_DIR).glob("*.json")
        if len(s := p.stem.rsplit("_", 1)[-1]) == 8 and s.isdigit()
    ]
    return max(ends, default=None)


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    import polars as pl  # noqa: PLC0415

    names = INDEX_NAMES  # the TRI is not in NSE's daily file: niftyindices' answers check it
    wrong: list[dict[str, Any]] = []
    unstored: list[dict[str, Any]] = []
    compared = Counter[str]()
    without_file = Counter[str]()
    for day in ctx.store.days(DATASET_INDEX):
        if not ctx.start <= day <= ctx.end:
            continue
        stored = {
            r.index_name: (r.open, r.high, r.low, r.close)
            for r in ctx.store.read_index(day)
            if r.index_name in names
        }
        data = _index_file(ctx, day)
        if data is None:
            for index in stored:
                without_file[index] += 1
            continue
        source = read_index_file(data.decode("utf-8-sig", errors="replace"), names)
        bad, missing = compare_index_day(day, stored, source)
        wrong += bad
        unstored += missing
        for index in stored:
            (compared if index in source else without_file)[index] += 1
    findings = []
    if wrong:
        findings.append(finding("index_level_mismatch", FAIL, wrong))
    if unstored:
        findings.append(finding("index_in_file_not_stored", FAIL, unstored))
    if without_file:
        findings.append(
            finding(
                "index_day_without_nse_file",
                WARN,
                [{"index": k, "days": v} for k, v in sorted(without_file.items())],
            )
        )
    tri = verify_tri(ctx.store, ctx.root / TRI_RAW_DIR, ctx.start, ctx.end, ctx.end)
    kept_to = _last_tri_day(ctx)
    for code, severity, items in (
        ("tri_mismatch", FAIL, [(d.isoformat(), str(a), str(b)) for d, a, b in tri.mismatched]),
        (
            "tri_not_in_niftyindices",
            FAIL,
            [d.isoformat() for d in tri.not_in_raw if kept_to is None or d <= kept_to],
        ),
        (
            "tri_after_last_kept_answer",
            WARN,
            [d.isoformat() for d in tri.not_in_raw if kept_to is not None and d > kept_to],
        ),
        ("tri_not_stored", FAIL, [d.isoformat() for d in tri.not_stored]),
        ("tri_year_without_file", WARN, tri.no_file),
    ):
        if items:
            findings.append(finding(code, severity, items))
    etf_path = ctx.root / "etf" / "daily.parquet"
    etf: dict[str, Any] = {"store": None}
    if etf_path.is_file():
        rows = pl.read_parquet(etf_path).filter(pl.col("date").is_between(ctx.start, ctx.end))
        symbols = sorted(set(rows["symbol"].to_list()))
        with ctx.store.connect() as con:
            raw = {
                (s, d): (o, h, low, c)
                for s, d, o, h, low, c in con.execute(
                    _RAW_ETF_SQL, [symbols, ctx.start, ctx.end]
                ).fetchall()
            }
        n, bad_etf, no_bar = compare_etf(rows.iter_rows(named=True), raw)
        etf = {"store": "etf/daily.parquet", "rows": rows.height, "compared": n}
        if bad_etf:
            findings.append(finding("etf_price_mismatch", FAIL, bad_etf))
        if no_bar:
            findings.append(finding("etf_day_without_raw_bar", WARN, no_bar))
    else:
        ctx.note("D7: no ETF store (etf/daily.parquet); ETF closes unchecked")
    summary = {
        "index_days_compared": dict(compared),
        "index_days_without_nse_file": dict(without_file),
        "tri_compared": tri.compared,
        "etf": etf,
    }
    return check_result("D7", summary, findings)
