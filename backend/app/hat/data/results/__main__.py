"""The financial-results pipeline's command line (docs/runbook.md, Financial results).

    python -m app.hat.data.results fetch [--from 2020-03-31] [--to YYYY-MM-DD] [--symbols A,B]
    python -m app.hat.data.results fetch-secondary [--symbols A,B]
    python -m app.hat.data.results parse [--from ...] [--to ...] [--symbols A,B]
    python -m app.hat.data.results derive
    python -m app.hat.data.results mark-failed

`fetch` downloads NSE's listings, announcements and XBRL documents into
`$HAT_DATA_ROOT/raw/nse-results/` at about one request a second (`fetch.py`); `fetch-secondary`
downloads the secondary source's company pages for the covered instruments into
`$HAT_DATA_ROOT/raw/secondary-results/` at one request every 3 s (`secondary.py`); `parse`
reads only the cache and appends what is new to `hat_financial_result` (`ingest.py`); `derive`
rebuilds `hat_financial_growth` from it (`derive.py`). Every step is incremental and can be
stopped and rerun. `--offline` makes `fetch` read the cache only. `--interval S` sets the pause
between requests (default 1 s, `HISTORY_INTERVAL` with `--history`). `--shard K/N` splits the
members between N runs, each paced on its own; run one at a time when NSE is slow. A run that
meets NSE's maintenance page `OUTAGE_LIMIT` times in a row stops (exit 2): rerun it later.
`mark-failed` marks the cached answers that are not what was asked for (`nse.failed`), so the
next `fetch` asks again.

`fetch --history` is the fundamentals backfill (`fetch.py`, docs/fundamentals.md): period ends
from `HISTORY_FROM` (unless `--from`), the NIFTY 500 members of any day from `HISTORY_SINCE`
(those loaded so far), cached listings reused, Annual listings and HTML results pages too;
`fetch-secondary --history` fetches those members' pages.

`--universe nifty500|ep` (fetch, fetch-secondary and parse) and `--basis consolidated-first|both`
(all steps; derive applies it to what is stored) set the scope (`scope.py`); defaults nifty500
and consolidated-first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ...settings import operator_settings
from ...time import trade_date_of, utc_now
from .scope import BASIS_CONSOLIDATED_FIRST, BASIS_RULES, UNIVERSE_NIFTY500, UNIVERSES

if TYPE_CHECKING:
    from ...settings import HatSettings
    from .universe import Member

FIRST_PERIOD_END = date(2020, 3, 31)  # owner, 2026-10-28: XBRL from the March 2020 quarter
UNIVERSE_SINCE = FIRST_PERIOD_END  # members from the first quarter the store holds
NSE_RAW = Path("raw/nse-results")
SECONDARY_RAW = Path("raw/secondary-results")
HISTORY_FROM = date(2012, 3, 31)  # owner, 2026-10-31: fundamentals history from FY2012's year end
HISTORY_SINCE = date(2012, 7, 1)  # ... for every NIFTY 500 member of a day from July 2012
HISTORY_INTERVAL = 2.0  # seconds between history requests: NSE throttled two 1 s runs
REPORT_DIR = Path("results")  # under the data root: the parse step's filing log


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.results", description=__doc__)
    p.add_argument("step", choices=["fetch", "fetch-secondary", "parse", "derive", "mark-failed"])
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    p.add_argument("--symbols", default="", help="comma-separated symbols (default: all)")
    p.add_argument("--offline", action="store_true", help="fetch: read the cache only")
    p.add_argument("--no-announcements", action="store_true", help="fetch: skip the days")
    p.add_argument("--shard", default="", help="K/N: every Nth member from the Kth (0-based)")
    p.add_argument("--interval", type=float, default=None, help="fetch: seconds per request")
    p.add_argument("--history", action="store_true", help="fetch(-secondary): the backfill")
    p.add_argument("--universe", choices=UNIVERSES, default=UNIVERSE_NIFTY500)
    p.add_argument("--basis", choices=BASIS_RULES, default=BASIS_CONSOLIDATED_FIRST)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = operator_settings()  # before the database module, which reads .env too
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.step == "derive":
        from .derive import derive

        _out(derive(args.basis))
        return
    from .raw import RawCache

    if args.step == "mark-failed":
        from .fetch import mark_failed

        _out(mark_failed(RawCache(settings.data_root / NSE_RAW)))
        return

    today = trade_date_of(utc_now())
    end = args.end or today
    history = args.history and args.step in ("fetch", "fetch-secondary")
    start = args.start or (HISTORY_FROM if history else FIRST_PERIOD_END)
    root = settings.data_root
    since = HISTORY_SINCE if history else UNIVERSE_SINCE
    covered = shard(_covered(settings, today, args.symbols, args.universe, since), args.shard)
    if args.step == "fetch":
        from .fetch import fetch_nse
        from .nse import DEFAULT_INTERVAL_SECONDS, Client, NseUnavailableError

        interval = args.interval or (HISTORY_INTERVAL if history else DEFAULT_INTERVAL_SECONDS)
        client = Client(RawCache(root / NSE_RAW), offline=args.offline, interval=interval)
        try:
            report = fetch_nse(
                client,
                covered,
                start,
                end,
                today,
                announcements=not args.no_announcements,
                basis=args.basis,
                history=history,
            )
        except NseUnavailableError as exc:
            logging.getLogger(__name__).error("stopped: NSE unavailable (%s); rerun later", exc)
            raise SystemExit(2) from exc
        _out(report)
    elif args.step == "fetch-secondary":
        from .secondary import fetch_secondary

        nse = RawCache(root / NSE_RAW)
        _out(fetch_secondary(RawCache(root / SECONDARY_RAW), covered, args.basis, nse))
    else:
        from .ingest import parse

        nse, secondary = RawCache(root / NSE_RAW), RawCache(root / SECONDARY_RAW)
        _out(parse(nse, secondary, covered, start, end, root / REPORT_DIR, args.basis))


def shard(items: list[Member], spec: str) -> list[Member]:
    """`items` narrowed to shard `K/N` (every Nth from the Kth, 0-based), so N runs can share a
    long fetch; all of them for an empty spec."""
    if not spec:
        return items
    k, n = (int(x) for x in spec.split("/"))
    if not 0 <= k < n:
        raise ValueError(f"bad shard {spec}")
    return items[k::n]


def _covered(
    settings: HatSettings,
    today: date,
    symbols: str,
    universe: str,
    since: date = UNIVERSE_SINCE,
) -> list[Member]:
    """The universe (`universe.members`, `scope.UNIVERSES`) from `since`, narrowed to `symbols`
    when given; a `since` before the store's own start is the history backfill's (`partial`)."""
    from app.database import db_session_scope

    from ..store import DataStore
    from .universe import members

    wanted = {s.strip().upper() for s in symbols.split(",") if s.strip()}
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with db_session_scope() as session:
        partial = since < UNIVERSE_SINCE
        covered = members(session, store, since, today, universe, partial=partial)
    if wanted:
        covered = [m for m in covered if wanted & set(m.symbols)]
    logging.info(
        "%d instruments covered (%d NIFTY 500)", len(covered), sum(m.nifty500 for m in covered)
    )
    return covered


def _out(value: object) -> None:
    sys.stdout.write(f"{value}\n")


if __name__ == "__main__":
    main()
