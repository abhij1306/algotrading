"""How many NIFTY 500 stock-days lose their 5-minute replay to the full-grid rule of
`backtest.intraday.on_basis` (every bar of the 09:15-15:25 grid present, the first open within
`CLOSE_5M_PCT` of the daily open), against the rule before it (bars on the grid, none required,
the last close within `CLOSE_5M_PCT` of the daily close), by year and liquidity bucket.

A stock-day is a point-in-time NIFTY 500 member (`hat_index_member`) with a stored daily bar,
on a session the 5-minute store covers, before the 2025 hold-out. Its liquidity bucket is its
20-session average traded value before the day (Rs crore). For each day with bars the report
counts: passes the old rule, passes the new one, lost (old passes, new fails) by cause
(`missing`: on the grid with buckets missing; `open`: full grid, first open off), and the
missing buckets of the lost days (how many, and whether the first or last bucket is missing).

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \\
        venv/bin/python scripts/research/bars5m_grid_loss.py [--out FILE.json]

Reads the stores and the database only; prints a Markdown table and writes JSON under
`reports/bt-verify/` when `--out` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import duckdb  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.data.audit import CLOSE_5M_PCT  # noqa: E402
from app.hat.models import HatEvent, HatIndexMember  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

HOLD_OUT_START = "2025-01-01"
GRID_BARS = 75  # 09:15 to 15:25
FIRST_MIN, LAST_MIN = 555, 925  # 09:15 and 15:25, minutes after midnight IST
RANGE_PCT = 0.1
_FLOATS = ("adv", "open_off", "close_off", "high_off", "low_off")
BUCKETS = ((0, 10), (10, 25), (25, 50), (50, 100), (100, None))  # ADV20, Rs crore
REPORTS = PROJECT_ROOT / "reports" / "bt-verify"
_REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json")

_SQL = """
with daily as (
    select instrument_id, date, open, high, low, close,
           avg(traded_value) over (partition by instrument_id order by date
                                   rows between 20 preceding and 1 preceding) / 1e7 as adv,
           count(*) over (partition by instrument_id order by date
                          rows between 20 preceding and 1 preceding) as adv_n
    from read_parquet($daily, hive_partitioning = true)
    where date >= date '2021-09-01' and date < cast($holdout as date) and series in ('EQ', 'BE')
),
five as (
    select instrument_id, cast(date as date) as date,
           count(*) as n,
           count(distinct bar_start) as n_distinct,
           count(*) filter (where (hour(s) * 60 + minute(s) - 555) % 5 = 0 and second(s) = 0
                               and hour(s) * 60 + minute(s) between 555 and 925) as on_grid,
           min(s) as first_start, max(s) as last_start,
           arg_min(open, bar_start) as first_open, arg_max(close, bar_start) as last_close,
           max(high) as high, min(low) as low
    from (select *, timezone('Asia/Kolkata', bar_start) as s
          from read_parquet($five, hive_partitioning = true))
    where date < cast($holdout as date)
    group by 1, 2
),
days as (select distinct date from five),
members as (
    select m.instrument_id, d.date from members_df m join days d
      on m.effective_from <= d.date and (m.effective_to is null or m.effective_to > d.date)
)
select year(m.date) as year, d.adv, d.adv_n, f.n, f.n_distinct, f.on_grid,
       hour(f.first_start) * 60 + minute(f.first_start) as first_min,
       hour(f.last_start) * 60 + minute(f.last_start) as last_min,
       abs(f.first_open / d.open - 1) * 100 as open_off,
       abs(f.last_close / d.close - 1) * 100 as close_off,
       abs(f.high / d.high - 1) * 100 as high_off, abs(f.low / d.low - 1) * 100 as low_off
from members m
join daily d on d.instrument_id = m.instrument_id and d.date = m.date
left join five f on f.instrument_id = m.instrument_id and f.date = m.date
"""


def bucket_of(adv: float | None, adv_n: int) -> str:
    """The ADV20 bucket label of a stock-day; `n/a` without 20 prior sessions."""
    if adv is None or adv_n < 20:
        return "n/a"
    for lo, hi in BUCKETS:
        if hi is None or adv < hi:
            return f"{lo}+" if hi is None else f"{lo}-{hi}"
    return "n/a"  # pragma: no cover - the last bucket is open


def classify(row: dict[str, Any]) -> tuple[str, int]:
    """(`no_bars`, `kept`, `lost_missing`, `lost_open`, `failed_both` or `new_only`, missing
    buckets) of one stock-day's 5-minute summary under the old and the new rule."""
    if row["n"] is None:
        return "no_bars", 0
    on_grid = row["on_grid"] == row["n"]
    old = on_grid and row["close_off"] is not None and row["close_off"] <= CLOSE_5M_PCT
    full = on_grid and row["n_distinct"] == row["n"] == GRID_BARS
    new = full and row["open_off"] is not None and row["open_off"] <= CLOSE_5M_PCT
    missing = GRID_BARS - row["n_distinct"] if on_grid else 0
    if old and new:
        return "kept", 0
    if old:
        return ("lost_open" if full else "lost_missing"), missing
    return ("new_only" if new else "failed_both"), missing


def in_range(row: dict[str, Any]) -> bool:
    """Whether the bars' high and low are within `RANGE_PCT` of the daily bar's."""
    return (
        row["high_off"] is not None
        and row["low_off"] is not None
        and max(row["high_off"], row["low_off"]) <= RANGE_PCT
    )


def _missing_band(missing: int) -> str:
    return "1" if missing == 1 else "2-5" if missing <= 5 else "6-20" if missing <= 20 else "21+"


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by year and bucket, and the lost days' missing buckets."""
    table: dict[tuple[int, str], dict[str, int]] = {}
    bands: dict[str, int] = {}
    edges = {"first_missing": 0, "last_missing": 0, "both_present": 0}
    # the bars' high and low within RANGE_PCT of the daily bar's: no traded extreme is missing
    ranges = {"kept": [0, 0], "lost_missing_both_edges": [0, 0]}
    for row in rows:
        kind, missing = classify(row)
        both = row["first_min"] == FIRST_MIN and row["last_min"] == LAST_MIN
        group = "kept" if kind == "kept" else "lost_missing_both_edges"
        if kind == "kept" or (kind == "lost_missing" and both):
            ranges[group][0] += 1
            ranges[group][1] += in_range(row)
        cell = table.setdefault((row["year"], bucket_of(row["adv"], row["adv_n"])), {})
        cell["member_days"] = cell.get("member_days", 0) + 1
        cell[kind] = cell.get(kind, 0) + 1
        if kind == "lost_missing":
            band = _missing_band(missing)
            bands[band] = bands.get(band, 0) + 1
            edges["first_missing"] += row["first_min"] != FIRST_MIN
            edges["last_missing"] += row["last_min"] != LAST_MIN
            edges["both_present"] += both
    return {
        "cells": [{"year": y, "bucket": b, **c} for (y, b), c in sorted(table.items())],
        "lost_missing_by_count": dict(sorted(bands.items())),
        "lost_missing_edges": edges,
        "range_within_pct": {k: {"days": v[0], "in_range": v[1]} for k, v in ranges.items()},
    }


def markdown(cells: list[dict[str, Any]], key: str) -> str:
    """One Markdown table of the cells summed by `key` (year or bucket)."""
    sums: dict[Any, dict[str, int]] = {}
    for cell in cells:
        total = sums.setdefault(cell[key], {})
        for k, v in cell.items():
            if k not in ("year", "bucket"):
                total[k] = total.get(k, 0) + v
    cols = ("member_days", "no_bars", "kept", "lost_missing", "lost_open", "new_only")
    lines = [f"| {key} | " + " | ".join(cols) + " | lost % of old passes |"]
    lines.append("|" + " --- |" * (len(cols) + 2))
    for k, total in sorted(sums.items(), key=lambda kv: str(kv[0])):
        lost = total.get("lost_missing", 0) + total.get("lost_open", 0)
        old = total.get("kept", 0) + lost
        share = f"{100 * lost / old:.1f}" if old else "-"
        lines.append(f"| {k} | " + " | ".join(str(total.get(c, 0)) for c in cols) + f" | {share} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", help=f"a report file name, written under {REPORTS}")
    args = parser.parse_args(argv)
    if args.out is not None and not _REPORT_NAME.fullmatch(args.out):
        raise SystemExit("refused: --out is a bare file name ending .json")
    root = operator_settings().data_root
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        members = [
            (str(m.instrument_id), m.effective_from, m.effective_to)
            for m in session.scalars(
                select(HatIndexMember).where(HatIndexMember.index_name == "NIFTY 500")
            )
        ]
    con = duckdb.connect()
    con.execute("set memory_limit = '1500MB'")
    con.execute("set threads = 2")
    con.execute("set preserve_insertion_order = false")
    con.execute(
        "create table members_df (instrument_id varchar, effective_from date, effective_to date)"
    )
    con.executemany("insert into members_df values (?, ?, ?)", members)
    cur = con.execute(
        _SQL,
        {
            "daily": str(root / "bars_daily_raw" / "*" / "*.parquet"),
            "five": str(root / "bars_5m" / "*" / "*.parquet"),
            "holdout": HOLD_OUT_START,
        },
    )
    names = [d[0] for d in cur.description]
    rows = [
        {
            k: (float(v) if k in _FLOATS and v is not None else v)
            for k, v in zip(names, r, strict=True)
        }
        for r in cur.fetchall()
    ]
    report = tally(rows)
    print(markdown(report["cells"], "year"))
    print()
    print(markdown(report["cells"], "bucket"))
    print()
    print("lost to missing buckets, by missing count:", report["lost_missing_by_count"])
    print("lost to missing buckets, edges:", report["lost_missing_edges"])
    print(f"high and low within {RANGE_PCT}% of the daily bar:", report["range_within_pct"])
    if args.out is not None:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
