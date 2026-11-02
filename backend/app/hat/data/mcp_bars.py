"""The daily bars from the NSE bhavcopy MCP, for a machine NSE's archive refuses (`nse_web`).

    python -m app.hat.data.mcp_bars --from YYYY-MM-DD --to YYYY-MM-DD [--dry-run]

The MCP's `get_stock_history` serves the CM bhavcopy's rows (symbol, series, open, high, low,
close, previous close, volume, traded value) per symbol, up to three months a call. A session's
bars are those rows of every symbol that can trade: the symbols of the last stored session and
the equity symbols of Fyers' public symbol master (`fyers_master`, which also names each
symbol's ISIN). The rows go through the bhavcopy parser's checks (`parse_mcp_day`: the equity
series only, a readable ISIN, numbers and row check, one row per ISIN by series rank) and
`bhavcopy.to_bar_rows` into the day's partition, source `mcp`, as the archive build writes it;
unknown ISINs are registered inactive (`history_build.DbCatalog`), and the symbol history is
seeded from the bars as `history_cli` does.

A session is written only whole: every symbol answered (rows or `mcp_no_data`), each row has an
ISIN, and the session has at least `MIN_SHARE` of the rows of the stored session before it (the
MCP loads a session's bhavcopy in the evening; before that it has no row of it). The rows a
session was built from are kept once under `raw/` (`raw_name`), so a rerun rebuilds from them and
asks nothing. Exit 0 when every session asked for is stored, else 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from ..money import MONEY_SCALE, exact
from .bhavcopy import BhavRow, ParsedDay
from .fyers_master import fyers_listings
from .identity import best_per_isin
from .mcp_client import CODE_NO_DATA, McpClient, McpError, parse_data_date
from .reference import EQUITY_SERIES, ISIN_RE
from .rowcheck import Candidate, row_ok
from .store import MAX_VOLUME

logger = logging.getLogger(__name__)

SOURCE_MCP = "mcp"  # `store.SOURCES`: a bar day read from the bhavcopy MCP
MAX_MONTHS = 3  # `get_stock_history` answers up to three months a call
# [A] A session's rows vary by a few dozen around 2,950 (Oct 2026); far fewer means the MCP has
# not loaded the whole bhavcopy of the day yet.
MIN_SHARE = 0.9
RAW_PREFIX = "mcp_bhavcopy_"


def raw_name(day: date) -> str:
    """The kept rows of a session under `raw/`."""
    return f"{RAW_PREFIX}{day.isoformat()}.json"


# --- the ISINs -------------------------------------------------------------------------------


def isin_lookup(
    listed: Mapping[tuple[str, str], str], stored: Mapping[str, str]
) -> Callable[[str, str], str | None]:
    """A row's ISIN: Fyers' for its (symbol, series), else Fyers' one ISIN for the symbol under
    any series (a series move the master has not caught), else the ISIN the symbol had in the
    last stored session; None when none names it."""
    by_symbol: dict[str, set[str]] = {}
    for (symbol, _series), isin in listed.items():
        by_symbol.setdefault(symbol, set()).add(isin)

    def isin_of(symbol: str, series: str) -> str | None:
        found = listed.get((symbol, series))
        if found is not None:
            return found
        isins = by_symbol.get(symbol, set())
        if len(isins) == 1:
            return next(iter(isins))
        return stored.get(symbol)

    return isin_of


def universe(listed: Mapping[tuple[str, str], str], stored: Mapping[str, str]) -> list[str]:
    """The symbols asked for: every Fyers equity symbol and every symbol of the last session."""
    return sorted({s for s, _ in listed} | set(stored))


def months_for(first: date, last: date) -> int:
    """The `months` one `get_stock_history` call ending at `last` needs to reach `first`; a span
    longer than `MAX_MONTHS` raises ValueError (the archive tools fill a longer gap)."""
    months = max(1, math.ceil(((last - first).days + 1) / 28))
    if months > MAX_MONTHS:
        raise ValueError("mcp_span_too_long")
    return months


# --- one session's rows -----------------------------------------------------------------------


@dataclass
class McpDay:
    """A session's parse and what kept it from being whole."""

    parsed: ParsedDay
    unmapped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, series), no ISIN


def _number(raw: object) -> object:
    return exact(raw, floats=True, places=MONEY_SCALE)


def _volume(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def parse_mcp_day(
    day: date, rows: Iterable[Mapping[str, object]], isin_of: Callable[[str, str], str | None]
) -> McpDay:
    """The session's `get_stock_history` rows as the bhavcopy parser reads a file
    (`bhavcopy.parse_bhavcopy`): the equity series only, the row's date the session's, an ISIN,
    readable numbers within the row check, then one row per ISIN by series rank. A row without
    an ISIN is listed in `unmapped`, never written."""
    out = McpDay(ParsedDay(day))
    parsed = out.parsed
    seen: set[tuple[str, str]] = set()
    kept: list[BhavRow] = []
    for raw in rows:
        series = str(raw.get("series") or "").strip().upper()
        symbol = str(raw.get("symbol") or "").strip().upper()
        if series not in EQUITY_SERIES:
            parsed.skipped_series += 1
            continue
        if parse_data_date(raw.get("date")) != day:
            parsed.wrong_date += 1
            continue
        isin = isin_of(symbol, series) if symbol else None
        if isin is None:
            out.unmapped.append((symbol, series))
            continue
        open_, high, low, close, prev = (
            _number(raw.get(k)) for k in ("open", "high", "low", "close", "prevClose")
        )
        volume = _volume(raw.get("volume"))
        if (
            not ISIN_RE.match(isin)
            or open_ is None
            or high is None
            or low is None
            or close is None
            or prev is None
            or volume is None
            or volume >= MAX_VOLUME
            or not row_ok(Candidate(day, open_, high, low, close, prev, volume), day)  # type: ignore[arg-type]
        ):
            parsed.invalid += 1
            continue
        if (isin, series) in seen:
            parsed.duplicates += 1
            continue
        seen.add((isin, series))
        value = exact(raw.get("totalTradedValue"), floats=True, places=MONEY_SCALE)
        kept.append(
            BhavRow(day, isin, series, symbol, open_, high, low, close, prev, volume, value)
        )  # type: ignore[arg-type]
    best = best_per_isin(kept, lambda r: r.isin, lambda r: r.series)
    parsed.other_series = len(kept) - len(best)
    parsed.rows = best
    return out


def rows_by_day(answers: Mapping[str, Sequence[Mapping[str, object]]]) -> dict[date, list[dict]]:
    """Every answered row by its date, each symbol's in answer order, symbols sorted."""
    out: dict[date, list[dict]] = {}
    for symbol in sorted(answers):
        for row in answers[symbol]:
            day = parse_data_date(row.get("date"))
            if day is not None:
                out.setdefault(day, []).append(dict(row))
    return out


def whole(rows: int, before: int | None) -> bool:
    """Whether a session of `rows` rows can be the whole bhavcopy: some rows, and at least
    `MIN_SHARE` of the previous stored session's when there is one."""
    return rows > 0 and (before is None or rows >= MIN_SHARE * before)


# --- the fetch --------------------------------------------------------------------------------


@dataclass
class Fetched:
    answers: dict[str, list[dict]] = field(default_factory=dict)  # symbol -> rows (no trade: [])
    failed: list[str] = field(default_factory=list)  # symbols whose call failed


def fetch_history(client: McpClient, symbols: Sequence[str], first: date, last: date) -> Fetched:
    """`get_stock_history` of each symbol over [first, last], up to four calls in flight (the
    client's limit). `mcp_no_data` is a symbol with no row there; any other error fails it."""
    months = months_for(first, last)

    def one(symbol: str) -> tuple[str, list[dict] | None]:
        try:
            return symbol, client.stock_history(symbol, last, months)
        except McpError as exc:
            return symbol, ([] if exc.code == CODE_NO_DATA else None)

    out = Fetched()
    for symbol, rows in client.map_calls([(lambda s=s: one(s)) for s in symbols]):
        if rows is None:
            out.failed.append(symbol)
        else:
            out.answers[symbol] = [r for r in rows if _in(r, first, last)]
    return out


def _in(row: Mapping[str, object], first: date, last: date) -> bool:
    day = parse_data_date(row.get("date"))
    return day is not None and first <= day <= last


# --- the command line -------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.mcp_bars", description=__doc__)
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    p.add_argument("--dry-run", action="store_true", help="fetch and parse, write nothing")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the MCP and the DB
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from ..settings import operator_settings, process_env

    settings = operator_settings()  # before the database module, which reads .env too
    from sqlalchemy import select

    from app.database import HatDataSessionLocal

    from ..ids import uuid7
    from ..models import HatMarket
    from .bhavcopy import to_bar_rows
    from .calendar import session_calendar
    from .fyers_master import download_nse_cm
    from .history_build import DbCatalog
    from .mcp_client import client_from_env
    from .renames import renames_from_bars
    from .store import DATASET_BARS, DataStore
    from .symbol_history import seed_symbol_history

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatDataSessionLocal() as session:
        calendar = session_calendar(session)
        market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
    stored_days = set(store.days(DATASET_BARS))
    todo = sorted(
        d
        for d in (calendar.trading_days() if calendar is not None else [])
        if args.start <= d <= args.end and d not in stored_days
    )
    if not todo:
        _say("mcp_bars: every session stored")
        return 0
    if market_id is None:
        sys.stderr.write("mcp_bars_no_market\n")
        return 2
    fresh = [d for d in todo if not store.raw_exists(raw_name(d))]
    by_day: dict[date, list[dict]] = {}
    isins: dict[str, str] = {}
    if fresh:
        client = client_from_env(process_env())
        if client is None:
            sys.stderr.write("mcp_not_configured: no bhavcopy MCP endpoint (.env.example)\n")
            return 2
        before = max((d for d in stored_days if d < fresh[0]), default=None)
        stored = _symbol_isins(store, before)
        listed = fyers_listings(download_nse_cm())
        symbols = universe(listed, stored)
        try:
            months_for(fresh[0], fresh[-1])
        except ValueError:
            sys.stderr.write("mcp_span_too_long: fill more than three months with history_cli\n")
            return 2
        _say(f"mcp_bars: {len(symbols)} symbols for {fresh[0]}..{fresh[-1]}", True)
        fetched = fetch_history(client, symbols, fresh[0], fresh[-1])
        if fetched.failed:
            _say(f"mcp_bars: {len(fetched.failed)} symbols failed: {fetched.failed[:20]}")
            return 1
        by_day = rows_by_day(fetched.answers)
        isin_of = isin_lookup(listed, stored)
        isins = {
            f"{r['symbol']}|{r['series']}": isin
            for rows in by_day.values()
            for r in rows
            if (isin := isin_of(str(r.get("symbol", "")), str(r.get("series", "")))) is not None
        }
    catalog = DbCatalog(HatDataSessionLocal, market_id)
    run_id = str(uuid7())
    code = 0
    for day in todo:
        if store.raw_exists(raw_name(day)):
            kept = json.loads(store.read_raw(raw_name(day)))
            rows, used = kept["rows"], kept["isins"]
        else:
            rows = by_day.get(day, [])
            used = {
                f"{r['symbol']}|{r['series']}": isins.get(f"{r['symbol']}|{r['series']}")
                for r in rows
            }
            used = {k: v for k, v in used.items() if v is not None}
        before = max((d for d in store.days(DATASET_BARS) if d < day), default=None)
        prior = store.read_bars(before).height if before is not None else None
        result = parse_mcp_day(day, rows, lambda s, r, used=used: used.get(f"{s}|{r}"))
        parsed = result.parsed
        line = (
            f"{day}: {len(rows)} rows, {len(parsed.rows)} bars, invalid {parsed.invalid},"
            f" other series {parsed.other_series}, unmapped {len(result.unmapped)}"
        )
        if result.unmapped or not whole(len(parsed.rows), prior):
            _say(f"{line}: not whole (previous session {prior} bars), not written")
            code = 1
            break  # a later session waits for this one, as the archive build's order does
        if args.dry_run:
            _say(f"{line} (dry run)")
            continue
        body = json.dumps({"rows": rows, "isins": used}, sort_keys=True).encode()
        store.store_raw(raw_name(day), body)
        unknown = [r for r in parsed.rows if catalog.lookup(r.isin) is None]
        if unknown:
            catalog.register(unknown)
        bars, unresolved = to_bar_rows(parsed, catalog.lookup, run_id, SOURCE_MCP)
        if unresolved or not bars:
            _say(f"{line}: {len(unresolved)} without an instrument, not written")
            code = 1
            break
        store.write_bars(day, bars)
        _say(f"{line}: written")
    if not args.dry_run:
        with HatDataSessionLocal() as session:
            seed_symbol_history(session, market_id, renames_from_bars(store))
            session.commit()
    return code


def _say(line: str, flush: bool = False) -> None:  # noqa: FBT001, FBT002 - a print
    sys.stdout.write(line + "\n")
    if flush:
        sys.stdout.flush()


def _symbol_isins(store, day: date | None) -> dict[str, str]:  # noqa: ANN001 - DataStore
    """Symbol -> ISIN of the stored session `day` (none without one)."""
    if day is None:
        return {}
    bars = store.read_bars(day)
    return dict(zip(bars["symbol"].to_list(), bars["isin"].to_list(), strict=True))


if __name__ == "__main__":
    sys.exit(main())
