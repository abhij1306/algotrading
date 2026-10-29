"""python -m app.hat.data.verify [--check D1 ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
[--report DIR] [--ca-data DIR] [--notices DIR] [--kotak DIR] [--fetch]: run the data checks
(`app.hat.data.verify`) under a read-only transaction, print each check's status and counts, and
write the report. Exit 2 when a check fails."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..audit import FAIL, SKIPPED, check_result, finding
from . import CHECKS, FIRST_DAY, report_doc, report_folder, report_target, write_report
from .context import Context
from .markdown import render
from .references import kept_bytes
from .snapshot import snapshot

MODULES = {
    "D1": "rawbars",
    "D2": "adjustments",
    "D3": "membership",
    "D4": "bars5m",
    "D5": "filings",
    "D6": "sessions",
    "D7": "series",
}

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.verify")
    parser.add_argument("--check", action="append", choices=sorted(CHECKS), dest="checks")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_DAY)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--report", type=Path, help="the report folder, under audits/")
    parser.add_argument("--ca-data", type=Path, help="NSE corporate-action and results pulls")
    parser.add_argument("--notices", type=Path, help="NIFTY 500 notices and archived lists")
    parser.add_argument("--kotak", type=Path, help="the Kotak raw 5-minute files")
    parser.add_argument("--fetch", action="store_true", help="download missing public references")
    return parser


def _skipped(code: str) -> dict[str, Any]:
    return {**check_result("", {}, [finding(code, SKIPPED, [])]), "status": SKIPPED}


def run_checks(ctx: Context, ids: Sequence[str]) -> dict[str, dict[str, Any]]:  # pragma: no cover
    """Each check in turn; one that raises is a failed check carrying the error's type, so the
    others still run and the report says which one broke."""
    results = {}
    for check_id in ids:
        began = time.monotonic()
        module = importlib.import_module(f"{__package__}.{MODULES[check_id]}")
        try:
            result = module.run(ctx)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            logger.exception("verify %s raised", check_id)
            result = check_result("", {}, [finding("check_error", FAIL, [type(exc).__name__])])
        result = {**result, "name": check_id, "title": CHECKS[check_id]}
        result["seconds"] = round(time.monotonic() - began, 1)
        results[check_id] = result
        sys.stdout.write(f"{check_id} {result['status'].upper()} {_counts(result)}\n")
        sys.stdout.flush()
    return results


def _counts(result: dict[str, Any]) -> str:
    return " ".join(f"{f['code']}={f['count']}" for f in result.get("findings", ())) or "-"


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ...models import HatMarket  # noqa: PLC0415
    from ...settings import operator_settings  # noqa: PLC0415
    from ..store import DATASET_BARS, DataStore  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parser().parse_args(argv)
    settings = operator_settings()
    if args.report is not None:
        try:
            report_target(settings.data_root, args.report)
        except ValueError as exc:
            sys.stderr.write(f"verify refused: {exc}\n")
            return 1
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    days = store.days(DATASET_BARS)
    end = args.end or (days[-1] if days else date.today())  # noqa: DTZ011 - a calendar date
    ids = sorted(set(args.checks or CHECKS))
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatMarket})
        ctx = Context(
            store,
            session,
            args.start,
            end,
            ca_data=args.ca_data,
            notices=args.notices,
            kotak=args.kotak,
            fetch=args.fetch,
        )
        results = run_checks(ctx, ids)
        judged = snapshot(settings.data_root, session, args.start, end)
        session.rollback()
    for group, n in sorted(ctx.unfetched.items()):
        ctx.note(f"{n} reference file(s) of raw/reference/{group}/ could not be fetched")
    ctx.note(f"raw/reference/ holds {kept_bytes(ctx) / 1e6:.1f} MB")
    created = datetime.now(UTC)
    doc = report_doc(results, args.start, end, created, judged)
    folder = args.report or report_folder(settings.data_root, created.date(), args.start, end, ids)
    _, md_path = write_report(settings.data_root, folder, doc, render(doc, ctx.log))
    sys.stdout.write(
        json.dumps(
            {"status": doc["status"], "digest": doc["digest"], "report": str(md_path)}, indent=1
        )
        + "\n"
    )
    return 2 if doc["status"] == FAIL else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
