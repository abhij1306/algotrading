"""The financial-results pipeline's command line (docs/runbook.md, Financial results).

    python -m app.hat.data.results fetch [--from 2020-03-31] [--to YYYY-MM-DD] [--symbols A,B]
    python -m app.hat.data.results fetch-secondary [--symbols A,B]
    python -m app.hat.data.results parse [--from ...] [--to ...] [--symbols A,B]
    python -m app.hat.data.results derive

`fetch` downloads NSE's listings, announcements and XBRL documents into
`$HAT_DATA_ROOT/raw/nse-results/` at about one request a second (`fetch.py`); `fetch-secondary`
downloads the secondary source's company pages for the covered instruments into
`$HAT_DATA_ROOT/raw/secondary-results/` at one request every 3 s (`secondary.py`); `parse`
reads only the cache and appends what is new to `hat_financial_result` (`ingest.py`); `derive`
rebuilds `hat_financial_growth` from it (`derive.py`). Every step is incremental and can be
stopped and rerun. `--offline` makes `fetch` read the cache only.

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
UNIVERSE_SINCE = date(2021, 1, 1)
NSE_RAW = Path("raw/nse-results")
SECONDARY_RAW = Path("raw/secondary-results")
REPORT_DIR = Path("results")  # under the data root: the parse step's filing log


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.results", description=__doc__)
    p.add_argument("step", choices=["fetch", "fetch-secondary", "parse", "derive"])
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_PERIOD_END)
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    p.add_argument("--symbols", default="", help="comma-separated symbols (default: all)")
    p.add_argument("--offline", action="store_true", help="fetch: read the cache only")
    p.add_argument("--no-announcements", action="store_true", help="fetch: skip the days")
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

    today = trade_date_of(utc_now())
    end = args.end or today
    root = settings.data_root
    covered = _covered(settings, today, args.symbols, args.universe)
    if args.step == "fetch":
        from .fetch import fetch_nse
        from .nse import Client

        client = Client(RawCache(root / NSE_RAW), offline=args.offline)
        announcements = not args.no_announcements
        report = fetch_nse(
            client, covered, args.start, end, today, announcements=announcements, basis=args.basis
        )
        _out(report)
    elif args.step == "fetch-secondary":
        from .secondary import fetch_secondary

        _out(fetch_secondary(RawCache(root / SECONDARY_RAW), covered, args.basis))
    else:
        from .ingest import parse

        nse, secondary = RawCache(root / NSE_RAW), RawCache(root / SECONDARY_RAW)
        _out(parse(nse, secondary, covered, args.start, end, root / REPORT_DIR, args.basis))


def _covered(settings: HatSettings, today: date, symbols: str, universe: str) -> list[Member]:
    """The universe (`universe.members`, `scope.UNIVERSES`), narrowed to `symbols` when given."""
    from app.database import db_session_scope

    from ..store import DataStore
    from .universe import members

    wanted = {s.strip().upper() for s in symbols.split(",") if s.strip()}
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with db_session_scope() as session:
        covered = members(session, store, UNIVERSE_SINCE, today, universe)
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
