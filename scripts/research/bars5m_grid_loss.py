"""How many NIFTY 500 stock-days `backtest.intraday.on_basis` replays on 5-minute bars, against
the full-grid rule before it, by year and liquidity bucket, and whether its empty buckets are
windows without a trade.

The rule (`edges`): the 09:15 and 15:25 bars present, every bar on the IST 5-minute grid once,
the 09:15 open within `CLOSE_5M_PCT` of the daily open. The rule before (`full`) wanted every
bucket of the grid. A stock-day is a point-in-time NIFTY 500 member (`hat_index_member`) with a
stored daily bar, on a session the 5-minute store covers. Its liquidity bucket is its 20-session
average traded value before the day (Rs crore). Each day with bars is one of:

- `full`: every bucket present, the open fits (both rules replay it);
- `back`: the edges present, interior buckets empty, the open fits (only the rule replays it);
- `no_first`, `no_last`, `no_edges`: the 09:15 bar, the 15:25 bar or both missing;
- `off_grid`: a bar off the grid or twice; `open_off`: on the grid, the 09:15 open off.

Whether an empty bucket is a window without a trade, two ways:

- daily volume: the 5-minute volume over the daily volume, on `full` days against `back` days
  (a bucket dropped with trades in it shows as volume missing on `back` days only);
- with `--kotak DIR` (the broker export, `raw_5m/<SYMBOL>.parquet`): each empty bucket of a
  `back` day against the export's row for it, traded (volume > 0), flat (volume 0) or absent.
  The export labels a bar by its end until it stops printing a traded 15:30 row (to 2022-01),
  by its start after; a day with a traded 15:30 row is read as end-labelled.

What the rule reads to choose a day: the 09:15 bar and open (known at 09:20), the grid (a
property of the stored file) and the 15:25 bar (known only at the close). The last is later
data, so the report sets the days refused for it alone (`no_last`) beside the replayed days:
how many, and how often they close at the day's high or low (a circuit lock leaves the close
without trades).

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \\
        venv/bin/python scripts/research/bars5m_grid_loss.py [--kotak DIR] [--out FILE.json]

Reads the stores, the export and the database only; prints Markdown tables and writes JSON
under `reports/bt-verify/` when `--out` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import duckdb  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.data.audit import CLOSE_5M_PCT  # noqa: E402
from app.hat.data.bars import BAR_WIDTH  # noqa: E402
from app.hat.data.sessions import SESSION_CLOSE, SESSION_OPEN  # noqa: E402
from app.hat.models import HatEvent, HatIndexMember  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

# the grid `on_basis` reads: the regular session's 5-minute bars, in minutes after midnight IST
FIRST_MIN = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
WIDTH_MIN = int(BAR_WIDTH.total_seconds()) // 60
LAST_MIN = SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute - WIDTH_MIN
GRID_BARS = (LAST_MIN - FIRST_MIN) // WIDTH_MIN + 1
KINDS = ("full", "back", "no_first", "no_last", "no_edges", "off_grid", "open_off")
_FLOATS = ("adv", "open_off", "vol_ratio", "ret")
BUCKETS = ((0, 10), (10, 25), (25, 50), (50, 100), (100, None))  # ADV20, Rs crore
VOLUME_BANDS = (1.0, 5.0)  # |5-minute volume / daily volume - 1|, percent
REPORTS = PROJECT_ROOT / "reports" / "bt-verify"
_REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json")

# one stored 5-minute day's summary, built a year at a time (the whole store at once spills)
_FIVE_SQL = """
insert into five_days
    select instrument_id, cast(date as date) as date,
           count(*) as n,
           count(distinct bar_start) as n_distinct,
           count(*) filter (where (hour(s) * 60 + minute(s) - $first) % $width = 0
                               and second(s) = 0
                               and hour(s) * 60 + minute(s) between $first and $last) as on_grid,
           bool_or(hour(s) * 60 + minute(s) = $first and second(s) = 0) as has_first,
           bool_or(hour(s) * 60 + minute(s) = $last and second(s) = 0) as has_last,
           arg_min(open, bar_start) as first_open,
           sum(volume) as volume
    from (select *, timezone('Asia/Kolkata', bar_start) as s
          from read_parquet($five, hive_partitioning = true)
          where date >= cast($start as date) and date < cast($stop as date))
    group by 1, 2
"""

_SQL = """
create temp table day_rows as
with daily as (
    select instrument_id, date, symbol, open, high, low, close, volume,
           avg(traded_value) over (partition by instrument_id order by date
                                   rows between 20 preceding and 1 preceding) / 1e7 as adv,
           count(traded_value) over (partition by instrument_id order by date
                          rows between 20 preceding and 1 preceding) as adv_n
    from read_parquet($daily, hive_partitioning = true)
    where date >= date '2021-09-01' and series in ('EQ', 'BE')
),
days as (select distinct date from five_days),
members as (
    select m.instrument_id, d.date from members_df m join days d
      on m.effective_from <= d.date and (m.effective_to is null or m.effective_to > d.date)
)
select m.instrument_id, m.date, d.symbol, year(m.date) as year, d.adv, d.adv_n,
       f.n, f.n_distinct, f.on_grid, f.has_first, f.has_last,
       abs(f.first_open / d.open - 1) * 100 as open_off,
       f.volume / nullif(d.volume, 0) as vol_ratio,
       d.close / d.open - 1 as ret,
       d.close = d.high as close_at_high, d.close = d.low as close_at_low
from members m
join daily d on d.instrument_id = m.instrument_id and d.date = m.date
left join five_days f on f.instrument_id = m.instrument_id and f.date = m.date
"""

# the empty buckets of the `back` days, against the export's row for each
_KOTAK_SQL = """
with back as (select instrument_id, date, symbol from back_days),
grid as (select unnest(range($first, $last + 1, $width)) as minute),
stored as (
    select f.instrument_id, cast(timezone('Asia/Kolkata', f.bar_start) as date) as date,
           hour(timezone('Asia/Kolkata', f.bar_start)) * 60
             + minute(timezone('Asia/Kolkata', f.bar_start)) as minute
    from read_parquet($five, hive_partitioning = true) f
    semi join back b on b.instrument_id = f.instrument_id and b.date = cast(f.date as date)
),
empty as (
    select b.instrument_id, b.date, b.symbol, g.minute
    from back b cross join grid g
    anti join stored s on s.instrument_id = b.instrument_id and s.date = b.date
                       and s.minute = g.minute
),
raw as (  -- the IST wall clock: DuckDB reads a zoned timestamp in its session zone
    select symbol, cast(s as date) as date, hour(s) * 60 + minute(s) as minute, volume
    from (select regexp_extract(filename, '([^/]+)\\.parquet$', 1) as symbol,
                 timezone('Asia/Kolkata', ts) as s, volume
          from read_parquet($kotak, filename = true))
    where cast(s as date) in (select distinct date from back)
),
labels as (
    select symbol, date, bool_or(minute = $close and volume > 0) as end_labelled
    from raw group by 1, 2
),
rows as (
    select r.symbol, r.date,
           r.minute - case when l.end_labelled then $width else 0 end as minute, r.volume
    from raw r join labels l using (symbol, date)
)
select year(e.date) as year,
       case when l.symbol is null then 'day_absent'
            when r.volume > 0 then 'traded'
            when r.volume = 0 then 'flat'
            else 'absent' end as kotak,
       count(*) as buckets
from empty e
left join labels l on l.symbol = e.symbol and l.date = e.date
left join rows r on r.symbol = e.symbol and r.date = e.date and r.minute = e.minute
group by all order by 1, 2
"""


def bucket_of(adv: float | None, adv_n: int) -> str:
    """The ADV20 bucket label of a stock-day; `n/a` without 20 prior sessions."""
    if adv is None or adv_n < 20:
        return "n/a"
    for lo, hi in BUCKETS:
        if hi is None or adv < hi:
            return f"{lo}+" if hi is None else f"{lo}-{hi}"
    return "n/a"  # pragma: no cover - the last bucket is open


def classify(row: dict[str, Any]) -> str:
    """`no_bars` or one of `KINDS`, of one stock-day's 5-minute summary."""
    if row["n"] is None:
        return "no_bars"
    if row["on_grid"] != row["n"] or row["n_distinct"] != row["n"]:
        return "off_grid"
    if not (row["has_first"] or row["has_last"]):
        return "no_edges"
    if not row["has_first"]:
        return "no_first"
    if not row["has_last"]:
        return "no_last"
    if row["open_off"] is None or row["open_off"] > CLOSE_5M_PCT:
        return "open_off"
    return "full" if row["n"] == GRID_BARS else "back"


def volume_fit(ratios: Iterable[float]) -> dict[str, Any]:
    """How close the 5-minute volume comes to the daily volume: the days, the median ratio and
    the share within each of `VOLUME_BANDS` percent."""
    found = sorted(ratios)
    if not found:
        return {"days": 0}
    out: dict[str, Any] = {"days": len(found), "median": round(median(found), 4)}
    for band in VOLUME_BANDS:
        within = sum(abs(r - 1) * 100 <= band for r in found)
        out[f"within_{band:g}pct"] = round(100 * within / len(found), 1)
    return out


def _selection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"days": 0}
    rets = sorted(r["ret"] for r in rows if r["ret"] is not None)
    return {
        "days": len(rows),
        "median_ret_pct": round(100 * median(rets), 3) if rets else None,
        "close_at_high_pct": round(
            100 * sum(bool(r["close_at_high"]) for r in rows) / len(rows), 1
        ),
        "close_at_low_pct": round(100 * sum(bool(r["close_at_low"]) for r in rows) / len(rows), 1),
    }


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by year and bucket, the volume fit of `full` and `back` days, the empty-bucket
    counts of `back` days and the days refused for the 15:25 bar alone beside the replayed."""
    table: dict[tuple[int, str], dict[str, int]] = {}
    ratios: dict[str, list[float]] = {"full": [], "back": []}
    empty: dict[str, int] = {}
    chosen: dict[str, list[dict[str, Any]]] = {"replayed": [], "no_last": []}
    for row in rows:
        kind = classify(row)
        cell = table.setdefault((row["year"], bucket_of(row["adv"], row["adv_n"])), {})
        cell["member_days"] = cell.get("member_days", 0) + 1
        cell[kind] = cell.get(kind, 0) + 1
        if kind in ratios and row["vol_ratio"] is not None:
            ratios[kind].append(row["vol_ratio"])
        if kind == "back":
            band = _empty_band(GRID_BARS - row["n"])
            empty[band] = empty.get(band, 0) + 1
        if kind in ("full", "back"):
            chosen["replayed"].append(row)
        elif kind == "no_last" and row["open_off"] is not None and row["open_off"] <= CLOSE_5M_PCT:
            chosen["no_last"].append(row)  # refused for the 15:25 bar alone
    return {
        "cells": [{"year": y, "bucket": b, **c} for (y, b), c in sorted(table.items())],
        "volume_fit": {k: volume_fit(v) for k, v in ratios.items()},
        "back_empty_buckets": dict(sorted(empty.items())),
        "selection": {k: _selection(v) for k, v in chosen.items()},
    }


def _empty_band(missing: int) -> str:
    return "1" if missing == 1 else "2-5" if missing <= 5 else "6-20" if missing <= 20 else "21+"


def markdown(cells: list[dict[str, Any]], key: str) -> str:
    """One Markdown table of the cells summed by `key` (year or bucket)."""
    sums: dict[Any, dict[str, int]] = {}
    for cell in cells:
        total = sums.setdefault(cell[key], {})
        for k, v in cell.items():
            if k not in ("year", "bucket"):
                total[k] = total.get(k, 0) + v
    cols = ("member_days", "no_bars", *KINDS)
    lines = [f"| {key} | " + " | ".join(cols) + " | replayed % before | replayed % now |"]
    lines.append("|" + " --- |" * (len(cols) + 3))
    for k, total in sorted(sums.items(), key=lambda kv: str(kv[0])):
        days = total.get("member_days", 0)
        full, back = total.get("full", 0), total.get("back", 0)
        before = f"{100 * full / days:.1f}" if days else "-"
        now = f"{100 * (full + back) / days:.1f}" if days else "-"
        lines.append(
            f"| {k} | " + " | ".join(str(total.get(c, 0)) for c in cols) + f" | {before} | {now} |"
        )
    return "\n".join(lines)


def kotak_table(rows: list[tuple[int, str, int]]) -> str:
    """The `back` days' empty buckets by year against the export's row for each."""
    kinds = ("flat", "absent", "traded", "day_absent")
    years: dict[int, dict[str, int]] = {}
    for year, kind, n in rows:
        years.setdefault(year, {})[kind] = n
    lines = ["| year | " + " | ".join(kinds) + " |", "|" + " --- |" * (len(kinds) + 1)]
    for year, counts in sorted(years.items()):
        lines.append(f"| {year} | " + " | ".join(str(counts.get(k, 0)) for k in kinds) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--kotak", type=Path, help="the broker export folder (holds raw_5m/)")
    parser.add_argument("--out", help=f"a report file name, written under {REPORTS}")
    args = parser.parse_args(argv)
    if args.out is not None and not _REPORT_NAME.fullmatch(args.out):
        raise SystemExit("refused: --out is a bare file name ending .json")
    if args.kotak is not None and not (args.kotak / "raw_5m").is_dir():
        raise SystemExit("refused: --kotak has no raw_5m/ folder")
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
    five = str(root / "bars_5m" / "*" / "*.parquet")
    grid = {"first": FIRST_MIN, "last": LAST_MIN, "width": WIDTH_MIN}
    con.execute(
        "create temp table five_days (instrument_id varchar, date date, n bigint,"
        " n_distinct bigint, on_grid bigint, has_first boolean, has_last boolean,"
        " first_open double, volume hugeint)"
    )
    for year in range(2021, date.today().year + 1):
        con.execute(
            _FIVE_SQL,
            {"five": five, "start": f"{year}-01-01", "stop": f"{year + 1}-01-01", **grid},
        )
    con.execute(_SQL, {"daily": str(root / "bars_daily_raw" / "*" / "*.parquet")})
    cur = con.execute("select * exclude (instrument_id, date, symbol) from day_rows")
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
    print("5-minute volume over daily volume:", report["volume_fit"])
    print("back days by empty buckets:", report["back_empty_buckets"])
    print("days chosen, by what the rule reads at the close:", report["selection"])
    if args.kotak is not None:
        con.execute(
            "create temp table back_days as select instrument_id, date, symbol from day_rows "
            "where n is not null and on_grid = n and n_distinct = n and has_first and has_last "
            "and n < $grid_bars and open_off <= $open_pct",
            {"grid_bars": GRID_BARS, "open_pct": CLOSE_5M_PCT},
        )
        found = con.execute(
            _KOTAK_SQL,
            {
                "five": five,
                "kotak": str(args.kotak / "raw_5m" / "*.parquet"),
                "close": LAST_MIN + WIDTH_MIN,  # the closing row an end label prints
                **grid,
            },
        ).fetchall()
        report["kotak_empty_buckets"] = [{"year": y, "kotak": k, "buckets": n} for y, k, n in found]
        print()
        print(kotak_table(found))
    if args.out is not None:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
