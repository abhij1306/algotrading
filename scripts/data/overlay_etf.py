"""Idle-cash ETF overlay, Phase 0: the data, gathered and built outside the database.

Usage (from the repo root, backend venv active; `kotak` needs `neo-api-client pyotp`):

    python scripts/data/overlay_etf.py --work DIR local        # HAT_DATA_ROOT + hat_live, read-only
    python scripts/data/overlay_etf.py --work DIR kotak        # daily and 5-minute bars, Kotak Neo
    python scripts/data/overlay_etf.py --work DIR indices      # TRIs and the 1D Rate Index
    python scripts/data/overlay_etf.py --work DIR dividends    # LIQUIDBEES dividends (the AMC)
    python scripts/data/overlay_etf.py --work DIR build [--expense-ratio 0.0069]
    python scripts/data/overlay_etf.py --work DIR store        # build/ into HAT_DATA_ROOT/etf/

`--work` (or `OVERLAY_DATA_DIR`) is a directory outside the repo. Every download is kept raw
under it, one file per request, and never overwritten: a rerun asks only for what is missing.

- `local` copies what HAT already stores: the ETFs' daily bhavcopy bars and the NIFTY 50 /
  NIFTY 500 TRI closes from `HAT_DATA_ROOT`, and the ETFs' corporate actions and the trading
  calendar from the database (`DB_*`, in a read-only transaction).
- `kotak` asks Kotak Neo's market data (login, scrip_master, historical_data; never an order
  API, about one call a second) for the ETFs' daily bars, 180 days a call, and 5-minute bars,
  30 days a call, over the five years the broker serves, plus the NIFTY 50 daily closes.
  Credentials come from the environment or `.env` through `kotak_5m.py` and are never printed.
- `indices` asks niftyindices (the index publisher) for the NIFTY 50 and NIFTY 500 TRI and the
  NIFTY 1D Rate Index, one calendar year a request.
- `dividends` asks the AMC's IDCW history for LIQUIDBEES.
- `build` writes `build/`: split-adjusted daily bars with every adjustment listed, the
  LIQUIDBEES total-return series (dividend-based where the dividends cover a day, else the
  1D Rate Index less the expense ratio, flagged by date range), the index series and
  `coverage.json` (per series: span, sessions, gaps against the trading calendar, sources).
- `store` writes `build/` and the raw 5-minute bars into the store a backtest's overlay reads,
  `HAT_DATA_ROOT/etf/` (`app.hat.backtest.etf_data`), and prints its manifest, with the
  `open_gaps` data check (the daily opens a sleeve fill avoids).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ETFS = ("NIFTYBEES", "GOLDBEES", "LIQUIDBEES")
FIVE_MIN_ETFS = ETFS  # the plan asks NIFTYBEES and LIQUIDBEES; GOLDBEES funds V4 sales too
START = date(2015, 1, 1)
EPOCH = date(2021, 1, 1)  # windows are anchored here, so a rerun asks for the same windows
DAILY_DAYS = 180  # the broker's cap for "D"
FIVE_MIN_DAYS = 30  # the broker's cap for 1/3/5-minute intervals
KOTAK_INDEX = "Nifty 50"  # historical_data takes a price index by this name; no TRI is served
JUMP = 0.25  # a daily close move beyond this is a split/consolidation candidate
TRI_INDICES = {"NIFTY 50 TRI": "NIFTY 50", "NIFTY 500 TRI": "NIFTY 500"}
RATE_INDEX = "NIFTY 1D RATE INDEX"
# The LIQUIDBEES expense ratio the proxy deducts, a year: the AMC's TER for the scheme (0.69% as
# of 2025-03-31). One rate for the whole window; the report flags it.
EXPENSE_RATIO = 0.0069
# The AMC's IDCW history (found behind mf.nipponindiaim.com's dividend summary page): one JSON
# answer per date range, dd/mm/yyyy, "Fromate" as the API spells it. LIQUIDBEES is scheme "LB".
NIMF_DIVIDENDS = (
    "https://api.nipponindiaim.com/v1.0/inveasy/services/digitals/FetchDividendDetails"
    "?Scheme=LB&plan=&Fromate={start:%d/%m/%Y}&Todate={end:%d/%m/%Y}"
)
DIVIDEND_YEARS = 5  # the page searches five years at a time
# The first day the dividend records are used: before it the AMC lists only two dates until
# 2019-12, and two different amounts for each date to 2020-03-31 (the dividend distribution
# tax regime), so the rate proxy covers the days before.
DIVIDEND_FROM = date(2020, 4, 1)


@dataclass(frozen=True)
class Work:
    root: Path

    @property
    def local(self) -> Path:
        return self.root / "local"

    @property
    def kotak(self) -> Path:
        return self.root / "kotak"

    @property
    def indices(self) -> Path:
        return self.root / "niftyindices"

    @property
    def dividends(self) -> Path:
        return self.root / "dividends"

    @property
    def build(self) -> Path:
        return self.root / "build"


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


@dataclass(frozen=True)
class Split:
    """A unit split or consolidation: prices before `ex_date` times `factor` (0.1 for 1:10)."""

    ex_date: date
    factor: float
    text: str


# ---------------------------------------------------------------------------------------------
# pure helpers


def fixed_windows(lo: date, hi: date, days: int, epoch: date = EPOCH) -> list[tuple[date, date]]:
    """[start, end] windows of `days` days anchored at `epoch` and clipped to [lo, hi], oldest
    first: the same calendar windows on every run, so cached answers are reused."""
    out = []
    k = (lo - epoch).days // days
    while True:
        start = epoch + timedelta(days=k * days)
        end = start + timedelta(days=days - 1)
        if start > hi:
            return out
        out.append((max(start, lo), min(end, hi)))
        k += 1


def adjust(bars: Sequence[Bar], splits: Iterable[Split]) -> list[tuple[Bar, float]]:
    """Each bar back-adjusted for every split on a later ex-date (prices times the factor,
    volume divided by it), with the cumulative factor applied."""
    out = []
    ordered = sorted(splits, key=lambda s: s.ex_date)
    for b in bars:
        f = 1.0
        for s in ordered:
            if b.day < s.ex_date:
                f *= s.factor
        out.append(
            (
                Bar(
                    b.day,
                    b.open * f,
                    b.high * f,
                    b.low * f,
                    b.close * f,
                    b.volume / f,
                    b.source,
                ),
                f,
            )
        )
    return out


def jumps(closes: Sequence[tuple[date, float]], limit: float = JUMP) -> list[tuple[date, float]]:
    """(day, close / previous close) where the move is beyond `limit` either way."""
    out = []
    for (_, prev), (day, close) in zip(closes, closes[1:], strict=False):
        r = close / prev
        if abs(r - 1) > limit:
            out.append((day, r))
    return out


def dividends_by_session(
    sessions: Sequence[date], dividends: Mapping[date, float]
) -> dict[date, float]:
    """Each dividend (by ex-date) summed onto the first session on or after its date: a daily
    dividend declared on a holiday goes ex at the next session. Dividends after the last
    session are left out."""
    out: dict[date, float] = {}
    i = 0
    for day in sorted(dividends):
        while i < len(sessions) and sessions[i] < day:
            i += 1
        if i == len(sessions):
            break
        out[sessions[i]] = out.get(sessions[i], 0.0) + dividends[day]
    return out


def total_return(
    closes: Sequence[tuple[date, float]], paid: Mapping[date, float]
) -> list[tuple[date, float]]:
    """A total-return index from 100 on the first close: each session's growth is
    (close + dividend going ex that session) / previous close, the dividend reinvested."""
    if not closes:
        return []
    out = [(closes[0][0], 100.0)]
    for (_, prev), (day, close) in zip(closes, closes[1:], strict=False):
        out.append((day, out[-1][1] * (close + paid.get(day, 0.0)) / prev))
    return out


def rate_proxy(
    rate: Sequence[tuple[date, float]], expense_ratio: float
) -> list[tuple[date, float]]:
    """The 1D Rate Index less a yearly expense ratio, accrued by calendar day, from 100."""
    if not rate:
        return []
    out = [(rate[0][0], 100.0)]
    for (prev_day, prev), (day, level) in zip(rate, rate[1:], strict=False):
        drag = (1 - expense_ratio) ** ((day - prev_day).days / 365)
        out.append((day, out[-1][1] * level / prev * drag))
    return out


def dividend_sessions(sessions: Sequence[date], first: date, last: date) -> set[date]:
    """The sessions whose one-day return the dividend records fully cover: every calendar day
    since the previous session lies in [first, last]."""
    return {
        d
        for prev, d in zip(sessions, sessions[1:], strict=False)
        if prev >= first - timedelta(days=1) and d <= last
    }


def parse_index_archive(lines: Iterable[str], name: str) -> list[tuple[date, float]]:
    """(day, close) rows of `name` in an index day file (`ind_close_all_DDMMYYYY.csv`: name,
    dd-mm-yyyy, open, high, low, close, ...); other rows and unreadable values are skipped."""
    out = []
    for row in csv.reader(lines):
        if len(row) < 6 or row[0].strip().upper() != name.upper():
            continue
        try:
            day = datetime.strptime(row[1].strip(), "%d-%m-%Y").date()
            close = float(row[5].replace(",", ""))
        except ValueError:
            continue
        if close > 0:
            out.append((day, close))
    return out


def fill(
    primary: Sequence[tuple[date, float]], other: Sequence[tuple[date, float]]
) -> tuple[list[tuple[date, float]], list[date], list[tuple[date, float, float]]]:
    """`primary` with the days only `other` has added: (merged, days added, days both have
    whose values differ by more than 0.005)."""
    have = dict(primary)
    added = sorted(d for d, _ in other if d not in have)
    diffs = [(d, have[d], v) for d, v in other if d in have and abs(have[d] - v) > 0.005]
    merged = sorted({**dict(other), **have}.items())
    return merged, added, diffs


def growth(series: Sequence[tuple[date, float]]) -> dict[date, float]:
    """Each day's level over the previous day's in the series (the first day has none)."""
    return {d: v / p for (_, p), (d, v) in zip(series, series[1:], strict=False)}


def splice(
    days: Sequence[date],
    primary: Mapping[date, float],
    proxy: Mapping[date, float],
) -> list[tuple[date, float, str]]:
    """A chained index from 100 over `days`: each day grows by the primary series' one-day
    growth where it has one, else the proxy's, and is tagged with the one used; a day neither
    has carries the level flat, tagged "none"."""
    out: list[tuple[date, float, str]] = []
    level = 100.0
    for i, day in enumerate(days):
        if i == 0:
            out.append((day, level, "start"))
            continue
        if day in primary:
            level, tag = level * primary[day], "dividend"
        elif day in proxy:
            level, tag = level * proxy[day], "proxy"
        else:
            tag = "none"
        out.append((day, level, tag))
    return out


def ranges(tagged: Iterable[tuple[date, str]], tag: str) -> list[tuple[date, date]]:
    """Maximal runs of consecutive entries carrying `tag`, as [first, last] days."""
    out: list[tuple[date, date]] = []
    run: list[date] = []
    for day, t in tagged:
        if t == tag:
            run.append(day)
        elif run:
            out.append((run[0], run[-1]))
            run = []
    if run:
        out.append((run[0], run[-1]))
    return out


def coverage(days: Iterable[date], sessions: Sequence[date], start: date) -> dict[str, Any]:
    """A series' span and its gaps: the trading sessions from `max(start, first day)` to its
    last day that it lacks, as runs; extra days it has that the calendar does not."""
    have = sorted(set(days))
    if not have:
        return {"first": None, "last": None, "days": 0, "missing": 0, "gaps": [], "extra": []}
    lo, hi = max(start, have[0]), have[-1]
    want = [s for s in sessions if lo <= s <= hi]
    got = set(have)
    tagged = [(s, "missing" if s not in got else "ok") for s in want]
    cal = set(sessions)
    return {
        "first": str(have[0]),
        "last": str(hi),
        "days": sum(1 for d in have if d >= lo),
        "missing": sum(1 for _, t in tagged if t == "missing"),
        "gaps": [[str(a), str(b)] for a, b in ranges(tagged, "missing")],
        "extra": [str(d) for d in have if d >= lo and d not in cal],
    }


def parse_niftyindices(data: bytes, date_key: str, value_key: str) -> list[tuple[date, float]]:
    """(day, value) rows of a niftyindices JSON answer, oldest first; a row without a date or
    a positive number is skipped. Raises ValueError when the answer is no JSON list."""
    rows = json.loads(data.decode("utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("not a series")
    out = []
    for row in rows:
        try:
            day = datetime.strptime(str(row.get(date_key, "")).strip(), "%d %b %Y").date()
            value = float(str(row.get(value_key, "")).replace(",", ""))
        except (AttributeError, ValueError):
            continue
        if value > 0:
            out.append((day, value))
    return sorted(set(out))


def parse_nimf_dividends(data: bytes) -> list[tuple[date, float]]:
    """(effective date, rupees a unit) rows of an AMC IDCW answer, in its order; "No data
    found" is an empty list. Raises ValueError when the answer is no such object."""
    doc = json.loads(data.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("not an IDCW answer")
    rows = doc.get("FetchDividendDetailsResponse") or []
    if not isinstance(rows, list):
        raise ValueError("not an IDCW answer")
    return [
        (date.fromisoformat(str(r["DividendEffectiveDate"])[:10]), float(r["DividendRetail"]))
        for r in rows
        if isinstance(r, dict) and r.get("DividendEffectiveDate")
    ]


def year_chunks(lo: date, hi: date, years: int) -> list[tuple[date, date]]:
    """[start, end] ranges of `years` calendar years from `lo`'s year, the last ending at hi."""
    out = []
    y = lo.year
    while y <= hi.year:
        out.append((date(y, 1, 1), min(date(y + years - 1, 12, 31), hi)))
        y += years
    return out


def latest_ingest(
    rows: Iterable[Mapping[str, Any]], key: Sequence[str] = ("symbol", "date")
) -> list[Mapping[str, Any]]:
    """The latest ingest run's row per `key`, as HAT's `bars_daily` view keeps (run ids are
    uuid v7, so their order is time order), in the order of the rows kept."""
    best: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for r in rows:
        k = tuple(r[c] for c in key)
        if k not in best or str(r["ingest_run_id"]) > str(best[k]["ingest_run_id"]):
            best[k] = r
    return list(best.values())


def kotak_bars(candles: Iterable[Sequence[Any]], source: str) -> list[Bar]:
    """Daily candles [ts, o, h, l, c, v] as bars by their IST date."""
    return [
        Bar(
            date.fromisoformat(str(c[0])[:10]),
            float(c[1]),
            float(c[2]),
            float(c[3]),
            float(c[4]),
            float(c[5]),
            source,
        )
        for c in candles
    ]


# ---------------------------------------------------------------------------------------------
# io


def _write_once(path: Path, data: bytes) -> None:
    """A raw download, written whole; an existing file is kept as it is."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _backend() -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))


def _yesterday() -> date:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=1)


# ---------------------------------------------------------------------------------------------
# commands


def cmd_local(work: Work) -> None:
    """The ETF bars and index closes HAT stores, and the ETFs' corporate actions and the
    calendar from the database, read-only. Kept as copied on the first run."""
    _backend()
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.dataset as ds  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415 - after the sys.path change
    from app.paths import HAT_DATA_ROOT  # noqa: PLC0415

    out = work.local
    out.mkdir(parents=True, exist_ok=True)
    bars_path = out / "etf_daily_bhavcopy.csv"
    if not bars_path.exists():
        t = ds.dataset(HAT_DATA_ROOT / "bars_daily_raw", format="parquet", partitioning="hive")
        df = t.to_table(filter=pc.field("symbol").isin(list(ETFS))).to_pandas()
        cols = ["date", "symbol", "isin", "series", "open", "high", "low", "close", "volume"]
        df = df.assign(date=df["date"].astype(str), ingest_run_id=df["ingest_run_id"].astype(str))
        rows = latest_ingest(df[[*cols, "ingest_run_id"]].to_dict("records"))
        rows.sort(key=lambda r: (r["symbol"], r["date"]))
        _write_csv(bars_path, cols, ([r[c] for c in cols] for r in rows))
    index_path = out / "index_daily.csv"
    if not index_path.exists():
        t = ds.dataset(HAT_DATA_ROOT / "index_daily", format="parquet", partitioning="hive")
        df = t.to_table(filter=pc.field("index_name").isin(["NIFTY 50", "NIFTY 500 TRI"]))
        df = df.to_pandas()
        df = df.assign(date=df["date"].astype(str))[["date", "index_name", "close"]]
        df.sort_values(["index_name", "date"]).to_csv(index_path, index=False)
    ca_sql = text(
        "select i.symbol, i.isin, ca.ex_date, ca.action_type, ca.adjustment_factor,"
        " ca.cash_amount, ca.raw_text, ca.source from hat_corporate_action ca"
        " join hat_instrument i on i.id = ca.instrument_id where i.symbol = any(:s)"
        " order by i.symbol, ca.ex_date"
    )
    cal_sql = text("select date from hat_trading_calendar where is_trading order by date")
    with engine.connect() as conn:
        conn.execute(text("set transaction read only"))
        actions = conn.execute(ca_sql, {"s": list(ETFS)}).all()
        sessions = conn.execute(cal_sql).all()
        conn.rollback()
    ca_path, cal_path = out / "corporate_actions.csv", out / "calendar.csv"
    if not ca_path.exists():
        header = ["symbol", "isin", "ex_date", "action_type", "factor", "cash", "text", "source"]
        _write_csv(ca_path, header, actions)
    if not cal_path.exists():
        _write_csv(cal_path, ["date"], sessions)
    rate_path = out / "rate_index_archive.csv"
    if not rate_path.exists():
        rate: set[tuple[date, float]] = set()
        for path in sorted((HAT_DATA_ROOT / "raw").glob("ind_close_all_*.csv")):
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                rate.update(parse_index_archive(f, RATE_INDEX))
        _write_csv(rate_path, ["date", "close"], sorted(rate))
    print(f"LOCAL {len(actions)} corporate actions, {len(sessions)} sessions; files in {out}")


def cmd_kotak(work: Work, interval: float) -> None:
    """Daily and 5-minute bars over the broker's five years, one raw file per window."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import kotak_5m as k  # noqa: PLC0415 - a sibling script

    kw = k.Work(work.kotak)
    work.kotak.mkdir(parents=True, exist_ok=True)
    client = k.login()
    k.refresh_scrips(client, kw)
    tokens = k.token_index(k.equity_scrips(kw.scrips))
    pace = k.Paced(client, interval)
    hi = _yesterday()
    floor = k.five_years_ago(hi + timedelta(days=1))
    jobs = [(s, tokens[s], "D", DAILY_DAYS) for s in ETFS]
    jobs.append(("NIFTY50", KOTAK_INDEX, "D", DAILY_DAYS))
    jobs += [(s, tokens[s], "5min", FIVE_MIN_DAYS) for s in FIVE_MIN_ETFS]
    probe: dict[str, Any] = {"floor": str(floor), "to": str(hi), "series": {}}
    for label, token, iv, days in jobs:
        n = got = 0
        first = last = None
        for start, end in fixed_windows(floor, hi, days):
            path = work.kotak / "raw" / label / iv / f"{start}_{end}.json"
            if not path.exists():
                candles = pace.candles(token, start, end, iv)
                _write_once(path, json.dumps({"token": token, "candles": candles}).encode())
                n += 1
            candles = json.loads(path.read_text())["candles"]
            if candles:
                got += len(candles)
                first = first or str(candles[0][0])[:10]
                last = str(candles[-1][0])[:10]
        probe["series"][f"{label}/{iv}"] = {"first": first, "last": last, "rows": got}
        print(f"{label} {iv}: {got} rows {first}..{last} ({n} calls)", flush=True)
    # one call before the floor shows the broker's own limit
    old = floor - timedelta(days=10)
    try:
        before = pace.candles(tokens["NIFTYBEES"], old, old + timedelta(days=5), "D")
        probe["before_floor"] = f"{len(before)} candles for {old}"
    except RuntimeError as e:
        probe["before_floor"] = str(e)[:200]
    (work.kotak / "probe.json").write_text(json.dumps(probe, indent=1))
    print(f"KOTAK DONE floor {floor}, {pace.calls} calls")


def cmd_indices(work: Work, interval: float) -> None:
    """The TRIs and the 1D Rate Index from niftyindices, one raw file per calendar year."""
    _backend()
    from app.hat.data.index_history import (  # noqa: PLC0415
        HISTORY_URL,
        TRI_URL,
        http_poster,
        tri_request,
    )

    post = http_poster()
    hi = _yesterday()
    jobs = [(name, base, TRI_URL) for name, base in TRI_INDICES.items()]
    jobs.append((RATE_INDEX, RATE_INDEX, HISTORY_URL))
    for name, base, url in jobs:
        calls = 0
        for year in range(START.year, hi.year + 1):
            start, end = date(year, 1, 1), min(date(year, 12, 31), hi)
            path = work.indices / name.replace(" ", "_") / f"{start:%Y%m%d}_{end:%Y%m%d}.json"
            if path.exists():
                continue
            time.sleep(interval)
            result = post(url, tri_request(base, start, end))
            calls += 1
            if result.status != "ok" or not result.data:
                sys.exit(f"INDICES BLOCKED: {name} {year} answered {result.status}")
            _write_once(path, result.data)
        print(f"{name}: {calls} calls", flush=True)
    print("INDICES DONE")


def cmd_dividends(work: Work, interval: float) -> None:
    """LIQUIDBEES' IDCW history from the AMC, one raw file per five-year range, then
    `liquidbees_dividends.csv` (date, per_unit, rows_that_day) from every row."""
    import urllib.request  # noqa: PLC0415

    hi = _yesterday()
    rows: list[tuple[date, float]] = []
    for start, end in year_chunks(date(2010, 1, 1), hi, DIVIDEND_YEARS):
        path = work.dividends / "raw" / f"{start:%Y%m%d}_{end:%Y%m%d}.json"
        if not path.exists():
            time.sleep(interval)
            url = NIMF_DIVIDENDS.format(start=start, end=end)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310 - fixed https URL
            with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - fixed https URL
                _write_once(path, r.read())
        rows += parse_nimf_dividends(path.read_bytes())
    per_day: dict[date, list[float]] = {}
    for d, v in rows:
        per_day.setdefault(d, []).append(v)
    _write_csv(
        work.dividends / "liquidbees_dividends.csv",
        ["date", "per_unit", "rows_that_day"],
        [
            [d, v[0] if len(v) == 1 else "", ";".join(map(str, v))]
            for d, v in sorted(per_day.items())
        ],
    )
    first, last = (min(per_day), max(per_day)) if per_day else (None, None)
    dual = sum(1 for v in per_day.values() if len(v) > 1)
    print(f"DIVIDENDS {len(rows)} rows, {len(per_day)} dates {first}..{last}, {dual} dual-amount")


# ---------------------------------------------------------------------------------------------
# build


def _index_files(work: Work, name: str, value_key: str) -> list[tuple[date, float]]:
    date_key = "Date" if value_key == "TotalReturnsIndex" else "HistoricalDate"
    rows: set[tuple[date, float]] = set()
    for path in sorted((work.indices / name.replace(" ", "_")).glob("*.json")):
        rows.update(parse_niftyindices(path.read_bytes(), date_key, value_key))
    return sorted(rows)


def _kotak_candles(work: Work, label: str, iv: str) -> list[list[Any]]:
    return unique_candles(
        json.loads(p.read_text())["candles"]
        for p in sorted((work.kotak / "raw" / label / iv).glob("*.json"))
    )


def unique_candles(files: Iterable[Sequence[Sequence[Any]]]) -> list[list[Any]]:
    """One candle per timestamp, in time order, from the window files oldest first. The current
    window is fetched again each day under a later end (`start_<yesterday>.json`), so its files
    overlap: the latest file's candle wins."""
    by_ts: dict[str, list[Any]] = {}
    for candles in files:
        for c in candles:
            by_ts[str(c[0])] = list(c)
    return [by_ts[ts] for ts in sorted(by_ts)]


def _db_bars(work: Work) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {s: [] for s in ETFS}
    for r in _read_csv(work.local / "etf_daily_bhavcopy.csv"):
        b = Bar(
            date.fromisoformat(r["date"]),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r["volume"]),
            f"bhavcopy:{r['isin']}",
        )
        out[r["symbol"]].append(b)
    return out


def _merge_kotak(db: list[Bar], kotak: list[Bar]) -> tuple[list[Bar], dict[str, Any]]:
    """The stored bars, then Kotak's for the days after the last stored day; the overlap's
    close differences beyond a paisa are counted."""
    have = {b.day: b for b in db}
    last = max(have) if have else date.min
    diffs = [
        (str(b.day), have[b.day].close, b.close)
        for b in kotak
        if b.day in have and abs(have[b.day].close - b.close) > 0.011
    ]
    overlap = sum(1 for b in kotak if b.day in have)
    merged = sorted(db + [b for b in kotak if b.day > last], key=lambda b: b.day)
    return merged, {"overlap_days": overlap, "close_mismatches": len(diffs), "examples": diffs[:5]}


def _five_min_coverage(candles: list[list[Any]], sessions: Sequence[date]) -> dict[str, Any]:
    per_day: dict[date, int] = {}
    for c in candles:
        d = date.fromisoformat(str(c[0])[:10])
        per_day[d] = per_day.get(d, 0) + 1
    cov = coverage(per_day, sessions, date(2022, 1, 1))
    counts = sorted(per_day.values())
    cov["bars"] = sum(counts)
    cov["short_sessions"] = {str(d): n for d, n in sorted(per_day.items()) if n < 75}
    cov["bars_per_session_median"] = counts[len(counts) // 2] if counts else 0
    return cov


def cmd_build(work: Work, expense_ratio: float) -> None:
    out = work.build
    out.mkdir(parents=True, exist_ok=True)
    sessions = [date.fromisoformat(r["date"]) for r in _read_csv(work.local / "calendar.csv")]
    sessions = [s for s in sessions if s <= _yesterday()]
    session_set = set(sessions)
    splits: dict[str, list[Split]] = {s: [] for s in ETFS}
    for r in _read_csv(work.local / "corporate_actions.csv"):
        if r["action_type"] in {"split", "consolidation"}:
            splits[r["symbol"]].append(
                Split(date.fromisoformat(r["ex_date"]), float(r["factor"]), r["text"])
            )
    summary: dict[str, Any] = {"built": str(date.today()), "series": {}, "adjustments": []}
    db = _db_bars(work)
    rows = []
    closes: dict[str, list[tuple[date, float]]] = {}
    for sym in ETFS:
        kotak = kotak_bars(_kotak_candles(work, sym, "D"), "kotak")
        merged, check = _merge_kotak(db[sym], kotak)
        merged = [b for b in merged if b.day >= START]
        adjusted = adjust(merged, splits[sym])
        raw_jumps = jumps([(b.day, b.close) for b in merged])
        adj_jumps = jumps([(b.day, b.close) for b, _ in adjusted])
        for s in splits[sym]:
            summary["adjustments"].append(
                {"symbol": sym, "ex_date": str(s.ex_date), "factor": s.factor, "text": s.text}
            )
        for b, f in adjusted:
            rows.append(
                [b.day, sym, *(round(x, 4) for x in (b.open, b.high, b.low, b.close)), b.volume]
                + [f, b.source]
            )
        closes[sym] = [(b.day, b.close) for b, _ in adjusted]
        cov = coverage([b.day for b in merged], sessions, START)
        cov |= {
            "kotak_check": check,
            "sources": sorted({b.source for b in merged}),
            "raw_jumps": [[str(d), round(r, 4)] for d, r in raw_jumps],
            "adjusted_jumps": [[str(d), round(r, 4)] for d, r in adj_jumps],
        }
        summary["series"][f"{sym} daily"] = cov
    header = ["date", "symbol", "open", "high", "low", "close", "volume", "factor", "source"]
    _write_csv(out / "etf_daily_adjusted.csv", header, rows)

    # indices
    stored = {
        name: sorted(
            (date.fromisoformat(r["date"]), float(r["close"]))
            for r in _read_csv(work.local / "index_daily.csv")
            if r["index_name"] == name
        )
        for name in ("NIFTY 50", "NIFTY 500 TRI")
    }
    nifty = [p for p in stored["NIFTY 50"] if p[0] >= START]
    k50 = kotak_bars(_kotak_candles(work, "NIFTY50", "D"), "kotak")
    have = {d for d, _ in nifty}
    nifty += [(b.day, b.close) for b in k50 if b.day > max(have)]
    nifty_close = dict(nifty)
    k50_diff = sum(
        1 for b in k50 if b.day in nifty_close and abs(nifty_close[b.day] - b.close) > 0.06
    )
    series = {
        "NIFTY 50": nifty,
        "NIFTY 50 TRI": _index_files(work, "NIFTY 50 TRI", "TotalReturnsIndex"),
        "NIFTY 500 TRI": _index_files(work, "NIFTY 500 TRI", "TotalReturnsIndex"),
    }
    archive = [
        (date.fromisoformat(r["date"]), float(r["close"]))
        for r in _read_csv(work.local / "rate_index_archive.csv")
        if date.fromisoformat(r["date"]) in session_set  # off-session rows hold stale values
    ]
    series[RATE_INDEX], rate_added, rate_diffs = fill(
        _index_files(work, RATE_INDEX, "CLOSE"), archive
    )
    on_sessions_rate = [(d, v) for d, v in series[RATE_INDEX] if d in session_set]
    stored500 = dict(stored["NIFTY 500 TRI"])
    tri500_diff = [
        (str(d), v, stored500[d])
        for d, v in series["NIFTY 500 TRI"]
        if d in stored500 and abs(stored500[d] - v) > 0.011
    ]
    index_rows = []
    for name, pts in series.items():
        index_rows += [[d, name, v] for d, v in pts if d >= START]
        cov = coverage([d for d, _ in pts], sessions, START)
        if name == "NIFTY 50":
            cov["sources"] = ["hat index_daily", "kotak Nifty 50 after the last stored day"]
            cov["kotak_close_mismatches"] = k50_diff
        elif name == "NIFTY 500 TRI":
            cov["sources"] = ["niftyindices"]
            cov["vs_stored_mismatches"] = len(tri500_diff)
            cov["vs_stored_examples"] = tri500_diff[:5]
        elif name == RATE_INDEX:
            cov["sources"] = ["niftyindices", "hat raw/ind_close_all_*.csv for its missing days"]
            cov["filled_from_archive"] = len([d for d in rate_added if d >= START])
            cov["filled_ranges"] = [
                [str(x), str(y)]
                for x, y in ranges(
                    [(d, "fill" if d in set(rate_added) else "") for d, _ in pts], "fill"
                )
                if y >= START
            ]
            cov["moves_beyond_0.2pct"] = [
                [str(d), round(r, 5)] for d, r in jumps(on_sessions_rate, 0.002)
            ]
            cov["archive_mismatches"] = len(rate_diffs)
            cov["archive_mismatch_examples"] = [[str(d), x, y] for d, x, y in rate_diffs[:5]]
        else:
            cov["sources"] = ["niftyindices"]
        summary["series"][name] = cov
    _write_csv(out / "index_daily.csv", ["date", "index", "close"], index_rows)

    # LIQUIDBEES total return
    liq = closes["LIQUIDBEES"]
    days = [d for d, _ in liq]
    div_rows = _dividends(work)
    paid = dividends_by_session(days, div_rows)
    covered = dividend_sessions(days, DIVIDEND_FROM, max(div_rows)) if div_rows else set()
    primary = {d: g for d, g in growth(total_return(liq, paid)).items() if d in covered}
    proxy = growth(rate_proxy(on_sessions_rate, expense_ratio))
    tr = splice(days, primary, proxy)
    _write_csv(
        out / "liquidbees_tr.csv",
        ["date", "close", "dividend", "tr_index", "basis"],
        [
            [d, c, paid.get(d, 0.0), round(v, 6), tag]
            for (d, c), (_, v, tag) in zip(liq, tr, strict=True)
        ],
    )
    tags = [(d, t) for d, _, t in tr]
    summary["series"]["LIQUIDBEES total return"] = {
        "first": str(days[0]),
        "last": str(days[-1]),
        "days": len(days),
        "dividend_days": sum(1 for _, t in tags if t == "dividend"),
        "proxy_days": sum(1 for _, t in tags if t == "proxy"),
        "flat_days": [[str(a), str(b)] for a, b in ranges(tags, "none")],
        "proxy_ranges": [[str(a), str(b)] for a, b in ranges(tags, "proxy")],
        "dividend_ranges": [[str(a), str(b)] for a, b in ranges(tags, "dividend")],
        "expense_ratio": expense_ratio,
        "dividend_records": len(div_rows),
        "dividend_from": str(DIVIDEND_FROM),
        "longest_dividend_gap_days": max(
            ((b - a).days for a, b in zip(sorted(div_rows), sorted(div_rows)[1:], strict=False)),
            default=None,
        ),
        "proxy_vs_dividend_by_year": _compare_years(primary, proxy),
        "cagr": _cagr(tr),
    }

    # 5-minute bars
    for sym in FIVE_MIN_ETFS:
        candles = _kotak_candles(work, sym, "5min")
        summary["series"][f"{sym} 5min"] = _five_min_coverage(candles, sessions)
    probe = work.kotak / "probe.json"
    summary["kotak_probe"] = json.loads(probe.read_text()) if probe.exists() else None
    (out / "coverage.json").write_text(json.dumps(summary, indent=1, default=str))
    print(f"BUILD DONE {out}")


def _cagr(tr: Sequence[tuple[date, float, str]]) -> float | None:
    years = (tr[-1][0] - tr[0][0]).days / 365.25
    return round((tr[-1][1] / tr[0][1]) ** (1 / years) - 1, 5) if years > 0 else None


def _compare_years(
    primary: Mapping[date, float], proxy: Mapping[date, float]
) -> dict[int, list[float]]:
    """Per year, [dividend-based return, proxy return] over the days both have."""
    out: dict[int, list[float]] = {}
    for d, g in primary.items():
        if d in proxy:
            acc = out.setdefault(d.year, [1.0, 1.0])
            acc[0] *= g
            acc[1] *= proxy[d]
    return {y: [round(a - 1, 5), round(b - 1, 5)] for y, (a, b) in sorted(out.items())}


def _dividends(work: Work) -> dict[date, float]:
    path = work.dividends / "liquidbees_dividends.csv"
    if not path.exists():
        return {}
    return {
        date.fromisoformat(r["date"]): float(r["per_unit"])
        for r in _read_csv(path)
        if date.fromisoformat(r["date"]) >= DIVIDEND_FROM
    }


def cmd_store(work: Work) -> None:
    _backend()
    from app.hat.backtest import etf_data  # noqa: PLC0415
    from app.paths import HAT_DATA_ROOT  # noqa: PLC0415

    found = etf_data.write(HAT_DATA_ROOT, work.build, work.kotak / "raw")
    print(json.dumps(found, indent=1, sort_keys=True))


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="overlay_etf.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--work",
        type=Path,
        default=os.environ.get("OVERLAY_DATA_DIR") or None,
        help="the working directory (default: $OVERLAY_DATA_DIR)",
    )
    p.add_argument("--interval", type=float, default=1.05, help="seconds between calls")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("local", "kotak", "indices", "dividends", "store"):
        sub.add_parser(name)
    b = sub.add_parser("build")
    b.add_argument("--expense-ratio", type=float, default=EXPENSE_RATIO)
    args = p.parse_args(argv)
    if args.work is None:
        p.error("--work or OVERLAY_DATA_DIR is required")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    work = Work(args.work.resolve())
    work.root.mkdir(parents=True, exist_ok=True)
    match args.cmd:
        case "local":
            cmd_local(work)
        case "kotak":
            cmd_kotak(work, args.interval)
        case "indices":
            cmd_indices(work, max(args.interval, 2.0))
        case "dividends":
            cmd_dividends(work, args.interval)
        case "build":
            cmd_build(work, args.expense_ratio)
        case "store":
            cmd_store(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
