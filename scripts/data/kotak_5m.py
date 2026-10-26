"""Download the broker's 5-minute history from Kotak Neo for the NIFTY 500 members, current and
former, as one Parquet file per symbol for `python -m app.hat.data.bars5m_import`.

Usage (from the repo root, backend venv active, `pip install neo-api-client pyotp` once):

    python scripts/data/kotak_5m.py --work DIR members            # member lists from hat_live
    python scripts/data/kotak_5m.py --work DIR probe [SYMBOL]     # earliest day served, one symbol
    python scripts/data/kotak_5m.py --work DIR download [--limit N]
    python scripts/data/kotak_5m.py --work DIR backfill
    python scripts/data/kotak_5m.py --work DIR summary
    python scripts/data/kotak_5m.py --work DIR former-map
    python scripts/data/kotak_5m.py --work DIR former-download [--limit N]
    python scripts/data/kotak_5m.py --work DIR former-trim
    python scripts/data/kotak_5m.py --work DIR former-summary

`--work` (or `KOTAK_5M_DIR`) is a directory outside the repo. `members` reads the database
(`DB_*`, read-only) and writes `universe_raw.csv` (today's members: symbol, isin, series,
instrument id, name; no header) and `former/former_members.csv` (members that left within the
five years the broker serves). Then `download` fetches every current member into
`raw_5m/<SYMBOL>.parquet` (columns ts (IST label), open, high, low, close, volume), resumable from
`state.json`; the importer reads `raw_5m/` with `universe_raw.csv` and the cached scrip master
`nse_cm_scrips.csv` beside it:

    python -m app.hat.data.bars5m_import --source DIR/raw_5m             # dry run, then --apply
    python -m app.hat.data.bars5m_import --source DIR/raw_5m_former \
        --universe DIR/former/universe_raw.csv

Former members: `former-map` maps each row to a token from the current scrip master by ISIN,
then by symbol with the same ISIN issuer (chars 4-7, so a reused symbol is skipped), then a
rename's single ordinary share of the issuer, then from the oldest archived scrip master the
broker still serves (`former/mapping.csv`). `former-download` writes `raw_5m_former/` and
`former/universe_raw.csv`, reusing a file `download` already fetched for the same token;
`former-summary` writes `former/unavailable.csv` with the reason for each row not served.

Market data only: the script calls login, scrip_master and historical_data, never an order API,
at about one call a second. Credentials come from the environment or the repo's `.env`
(`KOTAK_NEO_CONSUMER_KEY`; `KOTAK_NEO_MOBILE`, `_UCC`, `_MPIN`, `_TOTP_SECRET` for a TOTP login)
and are never printed or logged. Each file is written whole and atomically once its symbol
finishes, so a restart redoes only the symbol in flight.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")
EQUITY_GROUPS = ("EQ", "BE", "BL", "BZ", "SM", "ST")
STOCK_SERIES = ("EQ", "BE")  # BL rows are REIT units
WINDOW_DAYS = 30  # the backend's cap for 1/3/5-minute intervals
EMPTY_STOP = 3  # empty windows in a row before a symbol's history counts as exhausted
RELOGIN_SECONDS = 3 * 3600
ARCHIVE_FROM = date(2026, 6, 14)  # the oldest dated scrip master found was 2026-06-20
INDEX = "NIFTY 500"
UNMAPPED_REASON = (
    "no token: ISIN and symbol are in neither Kotak's current nor its oldest archived ({}) NSE "
    "scrip master; the history endpoint takes current tokens only"
)
ARCHIVED_REASON = "token {} is only in the archived scrip master ({}); history returns 422"


@dataclass(frozen=True)
class Work:
    """The working directory's layout."""

    root: Path

    @property
    def universe(self) -> Path:
        return self.root / "universe_raw.csv"

    @property
    def scrips(self) -> Path:
        return self.root / "nse_cm_scrips.csv"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def raw(self) -> Path:
        return self.root / "raw_5m"

    @property
    def former(self) -> Path:
        return self.root / "former"

    @property
    def raw_former(self) -> Path:
        return self.root / "raw_5m_former"


# ---------------------------------------------------------------------------------------------
# pure helpers


def ist_today() -> date:
    """The backend counts its 5-year limit from the IST date, which leads UTC by 5h30."""
    return datetime.now(IST).date()


def five_years_ago(today: date) -> date:
    """The oldest from-date the backend accepts ("fromDate cannot be older than 5 years")."""
    try:
        d = today.replace(year=today.year - 5)
    except ValueError:  # 29 Feb
        d = today.replace(year=today.year - 5, day=28)
    return d + timedelta(days=1)


def windows(newest: date, oldest: date) -> Iterator[tuple[date, date]]:
    """[start, end] request windows of at most WINDOW_DAYS, newest first, covering the range."""
    end = newest
    while end >= oldest:
        start = max(oldest, end - timedelta(days=WINDOW_DAYS - 1))
        yield start, end
        end = start - timedelta(days=1)


def redact(resp: object) -> str:
    """An error response with any token-like field dropped, short."""
    if isinstance(resp, dict):
        drop = {"token", "sid", "rid", "kid", "auth", "access_token", "data"}
        resp = {k: v for k, v in resp.items() if str(k).lower() not in drop}
    return str(resp)[:300]


def read_members(lines: Iterable[str]) -> list[dict[str, str]]:
    """`universe_raw.csv` rows (symbol, isin, series, instrument id, ...) that are stocks."""
    keys = ("symbol", "isin", "series", "instrument_id")
    rows = [dict(zip(keys, line.split(",")[:4], strict=False)) for line in lines if line.strip()]
    return [r for r in rows if r.get("series") in STOCK_SERIES]


def match_token(
    row: Mapping[str, str], scrips: Sequence[Mapping[str, str]]
) -> tuple[str, str] | None:
    """(token, how) for a `former_members.csv` row against scrip master rows: by ISIN, then by a
    symbol the row had with the same ISIN issuer, then the issuer's one ordinary share (a rename
    with a new ISIN)."""
    by_isin = {s["pISIN"]: s["pSymbol"] for s in scrips if s["pISIN"]}
    if row["isin"] in by_isin:
        return by_isin[row["isin"]], "isin"
    names = [row["symbol"], *row["member_symbols"].replace(";", " ").split()]
    for s in scrips:
        if s["pSymbolName"] in names and s["pISIN"][3:7] == row["isin"][3:7]:
            return s["pSymbol"], f"symbol:{s['pSymbolName']}:{s['pISIN']}"
    same = {
        (s["pSymbol"], s["pSymbolName"], s["pISIN"])
        for s in scrips
        if s["pGroup"] == "EQ" and s["pISIN"][:7] == row["isin"][:7] and s["pISIN"][7:9] == "01"
    }
    if len(same) == 1:
        tok, name, isin = same.pop()
        return tok, f"issuer:{name}:{isin}"
    return None


def _strip_rows(rows: csv.DictReader) -> Iterator[dict[str, str]]:
    rows.fieldnames = [c.strip() for c in rows.fieldnames or []]
    for r in rows:
        yield {k: (v or "").strip() for k, v in r.items()}


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(_strip_rows(csv.DictReader(f)))


def equity_scrips(path: Path) -> list[dict[str, str]]:
    return [r for r in csv_rows(path) if r.get("pGroup") in EQUITY_GROUPS]


def token_index(scrips: Iterable[Mapping[str, str]]) -> dict[str, str]:
    """Symbol and ISIN -> token, first row wins (the ISIN key finds a symbol renamed since)."""
    out: dict[str, str] = {}
    for row in scrips:
        out.setdefault(row["pSymbolName"], row["pSymbol"])
        if row.get("pISIN"):
            out.setdefault(row["pISIN"], row["pSymbol"])
    return out


def _load_state(path: Path, empty: dict[str, Any]) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else empty


def _save_state(path: Path, state: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)  # NOSONAR: path is <work>/state.json, never taken from the content


# ---------------------------------------------------------------------------------------------
# the broker


def _creds() -> dict[str, str]:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.utils.env_loader import load_dotenv  # noqa: PLC0415 - after the sys.path change

    load_dotenv()
    out = {k: v for k, v in os.environ.items() if k.startswith("KOTAK_NEO_") and v}
    if "KOTAK_NEO_CONSUMER_KEY" not in out:
        sys.exit("KOTAK BLOCKED: KOTAK_NEO_CONSUMER_KEY is not set")
    return out


def _ok(resp: object) -> bool:
    return isinstance(resp, dict) and "error" not in {str(k).lower() for k in resp}


def login() -> Any:  # noqa: ANN401 - the SDK client
    """A client; with a TOTP secret, logged in, else on the consumer key only (scrip master and
    history need only that)."""
    os.environ.setdefault("NEO_LOG_FILE_ENABLED", "false")
    os.environ.setdefault("NEO_LOG_LEVEL", "CRITICAL")
    from neo_api_client import NeoAPI  # noqa: PLC0415 - optional operator dependency

    c = _creds()
    client = NeoAPI(consumer_key=c["KOTAK_NEO_CONSUMER_KEY"], environment="prod")
    if not c.get("KOTAK_NEO_TOTP_SECRET"):
        return client
    import pyotp  # noqa: PLC0415 - optional operator dependency

    totp = pyotp.TOTP(c["KOTAK_NEO_TOTP_SECRET"].replace(" ", "").upper()).now()
    r1 = client.totp_login(mobile_number=c["KOTAK_NEO_MOBILE"], ucc=c["KOTAK_NEO_UCC"], totp=totp)
    if not (_ok(r1) and (r1.get("data") or {}).get("status") == "success"):
        sys.exit(f"KOTAK BLOCKED: totp_login failed ({redact(r1)})")
    r2 = client.totp_validate(mpin=c["KOTAK_NEO_MPIN"])
    if not (_ok(r2) and (r2.get("data") or {}).get("status") == "success"):
        sys.exit(f"KOTAK BLOCKED: totp_validate failed ({redact(r2)})")
    return client


def refresh_scrips(client: Any, work: Work) -> None:  # noqa: ANN401
    """Download today's NSE cash scrip master unless the cached copy is under a day old."""
    cache = work.scrips
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return
    url = client.scrip_master(exchange_segment="nse_cm")
    if not isinstance(url, str) or not url.startswith("https://"):
        sys.exit(f"KOTAK BLOCKED: scrip master ({redact(url)})")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - https checked above
        cache.write_bytes(r.read())


class Paced:
    """historical_data at most once per `min_interval` seconds, retrying transient failures."""

    def __init__(self, client: Any, min_interval: float) -> None:  # noqa: ANN401
        self.client, self.min_interval, self.last, self.calls = client, min_interval, 0.0, 0

    def candles(self, token: str, start: date, end: date) -> list[list[Any]]:
        r: object = None
        for attempt in range(8):
            wait = self.last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                r = self.client.historical_data(
                    neosymbol=f"nse_cm|{token}",
                    interval="5min",
                    from_date=start.isoformat(),
                    to_date=end.isoformat(),
                )
            except Exception as e:  # noqa: BLE001 - the SDK raises on 429 and 5xx
                r = {"Error": type(e).__name__, "msg": str(e)[:200]}
            if isinstance(r, dict) and str(r.get("status", "")).lower() == "success":
                return (r.get("data") or {}).get("candles") or []
            text = json.dumps(r, default=str)[:400].lower()
            if "no data" in text or "not found" in text or "older than" in text:
                return []
            if attempt >= 2 and "400" in text and "range" not in text and "429" not in text:
                raise RuntimeError(f"historical_data refused: {redact(r)}")
            time.sleep(min(60, 1.5 * 2**attempt))
        raise RuntimeError(f"historical_data kept failing: {redact(r)}")


def to_table(candles: list[list[Any]]) -> Any:  # noqa: ANN401 - a DataFrame
    import pandas as pd  # noqa: PLC0415

    df = pd.DataFrame(
        [c[:6] for c in candles], columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["ts"] = pd.to_datetime(df["ts"], format="ISO8601").dt.tz_convert("Asia/Kolkata")
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype("float64")
    df["volume"] = df["volume"].astype("int64")
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def history(pace: Paced, token: str, today: date, floor: date) -> list[Any]:
    """A token's history as tables, newest window first, until EMPTY_STOP empty windows in a row
    (past the listing day, or no history at all). Raises RuntimeError when a window keeps
    failing."""
    parts, empty = [], 0
    for start, end in windows(today, floor):
        c = pace.candles(token, start, end)
        if c:
            parts.append(to_table(c))
            empty = 0
        else:
            empty += 1
            if empty >= EMPTY_STOP:
                break
    return parts


def _write_parquet(df: Any, path: Path) -> None:  # noqa: ANN401
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _span(df: Any) -> list[Any]:  # noqa: ANN401
    """[first day, last day, rows] of a history table."""
    if df.empty:
        return [None, None, 0]
    return [str(df.ts.iloc[0].date()), str(df.ts.iloc[-1].date()), len(df)]


def _merged(parts: list[Any], before: date | None = None) -> Any:  # noqa: ANN401
    import pandas as pd  # noqa: PLC0415

    df = pd.concat(parts).drop_duplicates("ts").sort_values("ts")
    if before is not None:
        df = df[df.ts.dt.date < before]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------------------------
# commands


def cmd_members(work: Work, as_of: date) -> None:
    """Today's members and the members that left within the broker's five years, read-only."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415 - hat_live, after the sys.path change

    current_sql = text(
        "select i.symbol, i.isin, i.series, i.id, i.name from hat_index_member m"
        " join hat_instrument i on i.id = m.instrument_id where m.index_name = :ix"
        " and m.effective_from <= :d and coalesce(m.effective_to, '2100-01-01') > :d"
        " order by i.symbol"
    )
    former_sql = text(
        "with cur as (select instrument_id from hat_index_member where index_name = :ix"
        " and effective_from <= :d and coalesce(effective_to, '2100-01-01') > :d),"
        " past as (select instrument_id, min(effective_from) member_from,"
        " max(coalesce(effective_to, '2100-01-01')) member_to,"
        " string_agg(distinct symbol, ' ') member_symbols from hat_index_member"
        " where index_name = :ix and coalesce(effective_to, '2100-01-01') >= :floor"
        " group by instrument_id)"
        " select i.id, i.isin, i.symbol, p.member_symbols, i.series, i.active, i.delisted_on,"
        " p.member_from, p.member_to from past p join hat_instrument i on i.id = p.instrument_id"
        " where p.instrument_id not in (select instrument_id from cur) order by i.symbol"
    )
    params = {"ix": INDEX, "d": as_of, "floor": five_years_ago(as_of)}
    with engine.connect() as conn:
        conn.execute(text("set transaction read only"))
        current = conn.execute(current_sql, params).all()
        former = conn.execute(former_sql, params).all()
        conn.rollback()
    work.former.mkdir(parents=True, exist_ok=True)
    with work.universe.open("w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(current)
    with (work.former / "former_members.csv").open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "instrument_id",
                "isin",
                "symbol",
                "member_symbols",
                "series",
                "active",
                "delisted_on",
                "member_from",
                "member_to",
            ]
        )
        w.writerows(["t" if v is True else "f" if v is False else v for v in r] for r in former)
    print(f"members on {as_of}: {len(current)} current, {len(former)} former")


def cmd_probe(work: Work, symbol: str, interval: float) -> None:
    """Recent candles and the earliest window with data for one symbol."""
    client = login()
    refresh_scrips(client, work)
    token = token_index(equity_scrips(work.scrips)).get(symbol)
    if not token:
        sys.exit(f"KOTAK BLOCKED: {symbol} not in scrip master")
    pace = Paced(client, interval)
    today = ist_today()
    c = pace.candles(token, today - timedelta(days=6), today)
    print(
        f"recent: {len(c)} candles; first {c[0][0] if c else None} last {c[-1][0] if c else None}"
    )
    have, lo = 0, None
    for days in (60, 180, 365, 730, 1095, 1460, 1825):
        d = today - timedelta(days=days)
        if pace.candles(token, d, d + timedelta(days=6)):
            have = days
        else:
            lo = days
            break
    if lo is None:
        print(f"earliest: >= {have} days back (data at every probed lag)")
        return
    good, bad = have, lo
    while bad - good > 7:
        mid = (good + bad) // 2
        d = today - timedelta(days=mid)
        good, bad = (mid, bad) if pace.candles(token, d, d + timedelta(days=6)) else (good, mid)
    print(f"earliest window with data: {today - timedelta(days=good)} ({pace.calls} calls)")


def cmd_download(work: Work, limit: int | None, interval: float) -> None:
    work.raw.mkdir(parents=True, exist_ok=True)
    state = _load_state(work.state, {"done": {}, "missing": []})
    client = login()
    refresh_scrips(client, work)
    tokens = token_index(equity_scrips(work.scrips))
    pace = Paced(client, interval)
    today = ist_today()
    floor = five_years_ago(today)
    todo = [
        m
        for m in read_members(work.universe.read_text().splitlines())
        if m["symbol"] not in state["done"]
    ][:limit]
    print(f"{len(todo)} symbols to fetch, {len(state['done'])} done", flush=True)
    relogin_at = time.monotonic() + RELOGIN_SECONDS
    for i, m in enumerate(todo, 1):
        sym = m["symbol"]
        token = tokens.get(sym) or tokens.get(m["isin"])
        if not token:
            state["missing"] = sorted(set(state["missing"]) | {sym})
            _save_state(work.state, state)
            continue
        if time.monotonic() > relogin_at:
            pace.client = login()
            relogin_at = time.monotonic() + RELOGIN_SECONDS
        try:
            parts = history(pace, token, today, floor)
        except RuntimeError as e:  # left undone, so a rerun retries it
            print(f"[{i}/{len(todo)}] {sym}: FAILED {e}", flush=True)
            continue
        if parts:
            df = _merged(parts, before=today)  # whole sessions only
            if not df.empty:
                _write_parquet(df, work.raw / f"{sym}.parquet")
            state["done"][sym] = _span(df)
        else:
            state["done"][sym] = [None, None, 0]
        _save_state(work.state, state)
        d = state["done"][sym]
        print(
            f"[{i}/{len(todo)}] {sym}: {d[2]} rows {d[0]}..{d[1]} (calls {pace.calls})", flush=True
        )
    cmd_summary(work)


def cmd_backfill(work: Work, interval: float) -> None:
    """Fetch [floor, first) for done symbols whose history starts within one window of the
    floor: a run that crossed IST midnight computed the floor a day late, had its oldest window
    refused as too old and took it for empty. A later listing just returns nothing."""
    import pandas as pd  # noqa: PLC0415

    floor = five_years_ago(ist_today())
    state = _load_state(work.state, {"done": {}, "missing": []})
    todo = [
        (sym, date.fromisoformat(v[0]))
        for sym, v in state["done"].items()
        if v[0] and floor < date.fromisoformat(v[0]) <= floor + timedelta(days=WINDOW_DAYS + 1)
    ]
    print(f"{len(todo)} symbols to backfill from {floor}", flush=True)
    client = login()
    tokens = token_index(equity_scrips(work.scrips))
    isins = {m["symbol"]: m["isin"] for m in read_members(work.universe.read_text().splitlines())}
    pace = Paced(client, interval)
    for i, (sym, first) in enumerate(todo, 1):
        token = tokens.get(sym) or tokens.get(isins.get(sym, ""))
        if not token:
            continue
        try:
            c = pace.candles(token, floor, first - timedelta(days=1))
        except RuntimeError as e:
            print(f"[{i}/{len(todo)}] {sym}: FAILED {e}", flush=True)
            continue
        if c:
            path = work.raw / f"{sym}.parquet"
            df = _merged([to_table(c), pd.read_parquet(path)])
            _write_parquet(df, path)
            state["done"][sym] = _span(df)
            _save_state(work.state, state)
        d = state["done"][sym]
        print(f"[{i}/{len(todo)}] {sym}: +{len(c)} -> {d[2]} rows {d[0]}..{d[1]}", flush=True)
    cmd_summary(work)


def cmd_summary(work: Work) -> None:
    state = _load_state(work.state, {"done": {}, "missing": []})
    got = {s: v for s, v in state["done"].items() if v[2]}
    empty = sorted(s for s, v in state["done"].items() if not v[2])
    if state["missing"] or empty:
        print(f"no token: {state['missing']}; no data: {empty}")
    if not got:
        print("KOTAK DONE 0")
        return
    first = min(v[0] for v in got.values())
    last = max(v[1] for v in got.values())
    print(f"KOTAK DONE {len(got)} {first} {last} {sum(v[2] for v in got.values())}")


def oldest_archive(client: Any, work: Work) -> Path:  # noqa: ANN401
    """The oldest dated NSE cash scrip master the broker still serves, cached."""
    archive = work.former / "scrip_archive"
    cached = sorted(archive.glob("nse_cm_*.csv"))
    if cached:
        return cached[0]
    archive.mkdir(parents=True, exist_ok=True)
    cur = next(p for p in client.scrip_master()["filesPaths"] if "nse_cm" in p)
    u = urllib.parse.urlparse(cur)
    if u.scheme != "https":
        sys.exit("KOTAK BLOCKED: scrip master path is not https")
    for i in range(60):
        d = ARCHIVE_FROM + timedelta(days=i)
        url = u._replace(path=f"/wso2-scripmaster/v1/prod/{d}/transformed-v1/nse_cm-v1.csv")
        try:
            with urllib.request.urlopen(url.geturl(), timeout=120) as r:  # noqa: S310 - https
                path = archive / f"nse_cm_{d}.csv"
                path.write_bytes(r.read())
                return path
        except urllib.error.HTTPError:
            time.sleep(0.3)
    sys.exit("KOTAK BLOCKED: no archived scrip master found")


def cmd_former_map(work: Work) -> None:
    client = login()
    refresh_scrips(client, work)
    current = equity_scrips(work.scrips)
    archive = oldest_archive(client, work)
    old = equity_scrips(archive)
    out = []
    for row in csv_rows(work.former / "former_members.csv"):
        hit, src = match_token(row, current), "current"
        if not hit:
            hit, src = match_token(row, old), archive.stem
        out.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "symbol",
                        "isin",
                        "instrument_id",
                        "active",
                        "member_from",
                        "member_to",
                    )
                },
                "token": hit[0] if hit else "",
                "how": f"{src}:{hit[1]}" if hit else "",
            }
        )
    with (work.former / "mapping.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    n = sum(1 for r in out if r["token"])
    print(f"mapped {n} of {len(out)} rows; unmapped {len(out) - n}")


def former_plan(work: Work) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """(files, unavailable): one file per current token, named by its rows' symbol (the current
    name when a token has several), with the token's current ISIN, which the importer checks
    against the scrip master; a token already fetched by `download` is reused."""
    current = equity_scrips(work.scrips)
    isin_of = {s["pSymbol"]: s["pISIN"] for s in current}
    name_of = {s["pSymbol"]: s["pSymbolName"] for s in current}
    archived = sorted((work.former / "scrip_archive").glob("nse_cm_*.csv"))
    archive = archived[0].stem.removeprefix("nse_cm_") if archived else "none"
    tokens = token_index(current)
    members = read_members(work.universe.read_text().splitlines()) if work.universe.exists() else []
    raw = {tokens.get(m["symbol"]) or tokens.get(m["isin"]): m["symbol"] for m in members}
    files: dict[str, dict[str, Any]] = {}
    unavailable = []
    for row in csv_rows(work.former / "mapping.csv"):
        if not row["token"]:
            unavailable.append({**row, "reason": UNMAPPED_REASON.format(archive)})
        elif row["token"] not in isin_of:
            unavailable.append({**row, "reason": ARCHIVED_REASON.format(row["token"], archive)})
        else:
            files.setdefault(row["token"], {"token": row["token"], "rows": []})["rows"].append(row)
    for f in files.values():
        symbols = {r["symbol"] for r in f["rows"]}
        f["file"] = symbols.pop() if len(symbols) == 1 else name_of[f["token"]]
        f["isin"] = isin_of[f["token"]]
        f["reuse"] = raw.get(f["token"])
    return list(files.values()), unavailable


def cmd_former_download(work: Work, limit: int | None, interval: float) -> None:
    import pyarrow.parquet as pq  # noqa: PLC0415

    files, _ = former_plan(work)
    work.raw_former.mkdir(parents=True, exist_ok=True)
    with (work.former / "universe_raw.csv").open("w", newline="") as fh:
        csv.writer(fh).writerows([f["file"], f["isin"], "EQ"] for f in files)
    state_path = work.former / "state.json"
    state = _load_state(state_path, {"done": {}})
    todo = [f for f in files if f["file"] not in state["done"]][:limit]
    print(f"{len(todo)} files to fetch, {len(state['done'])} done", flush=True)
    today = ist_today()
    floor = five_years_ago(today)
    pace = Paced(login(), interval)
    for i, f in enumerate(todo, 1):
        target = work.raw_former / f"{f['file']}.parquet"
        if f["reuse"]:
            # copy2 keeps the mtime: the day the history is adjusted to
            shutil.copy2(work.raw / f"{f['reuse']}.parquet", target)
            ts = pq.read_table(target, columns=["ts"])["ts"]
            span = [str(ts[0].as_py().date()), str(ts[-1].as_py().date()), len(ts)]
            how = f"copy of raw_5m/{f['reuse']}"
        else:
            try:
                parts = history(pace, f["token"], today, floor)
            except RuntimeError as e:  # left undone, so a rerun retries it
                print(f"[{i}/{len(todo)}] {f['file']}: FAILED {e}", flush=True)
                continue
            span = [None, None, 0]
            if parts:
                df = _merged(parts, before=today)
                if not df.empty:
                    _write_parquet(df, target)
                span = _span(df)
            how = f"calls {pace.calls}"
        state["done"][f["file"]] = span
        _save_state(state_path, state)
        print(
            f"[{i}/{len(todo)}] {f['file']}: {span[2]} rows {span[0]}..{span[1]} ({how})",
            flush=True,
        )
    cmd_former_summary(work)


def cmd_former_trim(work: Work) -> None:
    """Drop bars of the IST day in progress from files fetched during market hours."""
    import pandas as pd  # noqa: PLC0415

    today = ist_today()
    state_path = work.former / "state.json"
    state = _load_state(state_path, {"done": {}})
    for path in sorted(work.raw_former.glob("*.parquet")):
        if state["done"].get(path.stem, [0, ""])[1] != str(today):
            continue
        df = pd.read_parquet(path)
        df = df[df.ts.dt.date < today].reset_index(drop=True)
        _write_parquet(df, path)
        state["done"][path.stem] = _span(df)
        _save_state(state_path, state)
        print(f"trimmed {path.stem} to {state['done'][path.stem][1]}", flush=True)


def cmd_former_summary(work: Work) -> None:
    files, unavailable = former_plan(work)
    done = _load_state(work.former / "state.json", {"done": {}})["done"]
    for f in files:
        d = done.get(f["file"])
        if d and not d[2]:
            unavailable += [
                {**r, "reason": "no 5m history returned for the token"} for r in f["rows"]
            ]
    with (work.former / "unavailable.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "isin", "reason"])
        w.writerows([r["symbol"], r["isin"], r["reason"]] for r in unavailable)
    pending = [f["file"] for f in files if f["file"] not in done]
    total = len(csv_rows(work.former / "former_members.csv"))
    rows = sum(done[f["file"]][2] for f in files if f["file"] in done)
    got = sum(len(f["rows"]) for f in files if done.get(f["file"], [0, 0, 0])[2])
    if pending:
        print(f"KOTAK FORMER PROGRESS {got} of {total}, {len(pending)} files pending, {rows} rows")
    else:
        print(f"KOTAK FORMER DONE {got} of {total} {len(unavailable)} {rows}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kotak_5m.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--work",
        type=Path,
        default=os.environ.get("KOTAK_5M_DIR") or None,
        help="the working directory (default: $KOTAK_5M_DIR)",
    )
    p.add_argument("--interval", type=float, default=1.05, help="seconds between history calls")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("members", help="write the member lists from hat_live (read-only)")
    m.add_argument("--as-of", type=date.fromisoformat, default=None, help="default: today (IST)")
    sub.add_parser("probe").add_argument("symbol", nargs="?", default="RELIANCE")
    for name in ("download", "former-download"):
        sub.add_parser(name).add_argument("--limit", type=int, default=None)
    for name in ("backfill", "summary", "former-map", "former-trim", "former-summary"):
        sub.add_parser(name)
    args = p.parse_args(argv)
    if args.work is None:
        p.error("--work or KOTAK_5M_DIR is required")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    work = Work(args.work.resolve())
    work.root.mkdir(parents=True, exist_ok=True)
    match args.cmd:
        case "members":
            cmd_members(work, args.as_of or ist_today())
        case "probe":
            cmd_probe(work, args.symbol, args.interval)
        case "download":
            cmd_download(work, args.limit, args.interval)
        case "backfill":
            cmd_backfill(work, args.interval)
        case "summary":
            cmd_summary(work)
        case "former-map":
            cmd_former_map(work)
        case "former-download":
            cmd_former_download(work, args.limit, args.interval)
        case "former-trim":
            cmd_former_trim(work)
        case "former-summary":
            cmd_former_summary(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
