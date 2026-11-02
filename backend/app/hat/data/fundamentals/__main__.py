"""The fundamentals store's command line (docs/fundamentals.md, docs/runbook.md).

    python -m app.hat.data.fundamentals extract [--symbols A,B] [--from 2012-03-31] [--to ...]
    python -m app.hat.data.fundamentals derive
    python -m app.hat.data.fundamentals report [--out PATH] [--example RELIANCE]
    python -m app.hat.data.fundamentals panel

`extract` reads the raw results cache only (`$HAT_DATA_ROOT/raw/nse-results/` and
`raw/secondary-results/`, never the network) and replaces `hat_fundamental_filing` and
`hat_fundamental_fact` (only the given symbols' rows with `--symbols`); its outcome counts go to
`$HAT_DATA_ROOT/fundamentals/extract-outcomes.json`. `derive` replaces `hat_fundamental_metric`
from them and the daily closes. `report` writes the coverage report. `panel` reads the store
(only) and writes the research panel and its report (`research.py`) under
`$HAT_DATA_ROOT/fundamentals/`. Every step is rerunnable and
gives the same rows from the same cache. The scope is the NIFTY 500 point-in-time members of any
day from July 2012 (`results.__main__.HISTORY_SINCE`; those loaded so far), consolidated-first
(`results/scope.py`), period ends from FY2012's year end.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from ...settings import operator_settings
from ...time import trade_date_of, utc_now
from ..results.__main__ import HISTORY_FROM, HISTORY_SINCE, NSE_RAW, SECONDARY_RAW
from ..results.scope import BASIS_CONSOLIDATED_FIRST, UNIVERSE_NIFTY500

OUT_DIR = Path("fundamentals")  # under the data root
OUTCOMES = "extract-outcomes.json"
PRICE_SINCE = date(2010, 1, 1)
EXAMPLE = "RELIANCE"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.fundamentals", description=__doc__)
    p.add_argument("step", choices=["extract", "derive", "report", "panel"])
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=HISTORY_FROM)
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    p.add_argument("--symbols", default="", help="extract: comma-separated symbols (default all)")
    p.add_argument("--out", type=Path, default=None, help="report: the Markdown file")
    p.add_argument("--example", default=EXAMPLE, help="report: the company sheet's symbol")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = operator_settings()  # before the database module, which reads .env too
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    root = settings.data_root
    out_dir = root / OUT_DIR
    today = trade_date_of(utc_now())
    from ..store import DataStore

    store = DataStore(root, memory_limit=settings.duckdb_memory)
    if args.step == "extract":
        from ..results.raw import RawCache
        from .extract import extract

        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        members = _members(store, today, wanted)
        report = extract(
            RawCache(root / NSE_RAW),
            RawCache(root / SECONDARY_RAW),
            members,
            (args.start, args.end or today),
            BASIS_CONSOLIDATED_FIRST,
            replace_all=not wanted,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / OUTCOMES).write_text(json.dumps(dict(report.outcomes), indent=1, sort_keys=True))
        _out(report)
    elif args.step == "panel":
        from .research import run

        _out(f"wrote {run(store, out_dir, HISTORY_SINCE, today)}")
    elif args.step == "derive":
        from .derive import derive

        _out(derive(store, PRICE_SINCE))
    else:
        from app.database import db_session_scope

        from .report import build

        outcomes_path = out_dir / OUTCOMES
        outcomes = json.loads(outcomes_path.read_text()) if outcomes_path.exists() else None
        target = args.out or out_dir / f"coverage-{today}.md"
        members = len(_members(store, today, set()))
        with db_session_scope() as session:
            text = build(session, args.example.upper(), members, outcomes)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _out(f"wrote {target}")


def _members(store: object, today: date, wanted: set[str]) -> list:  # pragma: no cover
    from app.database import db_session_scope

    from ..results.universe import members

    with db_session_scope() as session:
        covered = members(
            session,
            store,  # type: ignore[arg-type]
            HISTORY_SINCE,
            today,
            UNIVERSE_NIFTY500,
            partial=True,
        )
    if wanted:
        covered = [m for m in covered if wanted & set(m.symbols)]
    logging.info("%d instruments covered", len(covered))
    return covered


def _out(value: object) -> None:
    sys.stdout.write(f"{value}\n")


if __name__ == "__main__":
    main()
