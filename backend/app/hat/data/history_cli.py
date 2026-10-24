"""The historical build's command line (HAT-DATA-090..097, HAT-DEC-I2).

    python -m app.hat.data.history_cli [--from 2011-06-22] [--to YYYY-MM-DD] [--resume]

Runs `history_build.run_history_build` by hand, once, against the real NSE archive and the two
MCP servers: about 4,000 bhavcopy zips at about one per second (roughly 70 minutes), then
corporate actions, index history and a check of 50 random symbol-days against
`get_stock_history`. `--resume` skips every day already downloaded or known to have no file.

Environment: `HAT_DATA_ROOT` (data directory); the NSE bhavcopy MCP endpoint variable of the ops
spec (corporate actions, recent index history and validation; without it those steps are
skipped and the index comes from NSE's daily index archive alone); a PostgreSQL database as for
the rest of HAT (`DB_*`). Add `--skip-actions`, `--skip-index` or
`--skip-validation` to leave a step out. The recorded past holiday lists are stored first
(`calendar.store_recorded_history`), so a past holiday reads as a holiday and a weekend session is
fetched. A run stopped by Ctrl-C or an error still prints and saves its report, with the reason.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from ..settings import operator_settings, process_env
from ..time import IST
from . import corporate_actions as ca
from .bhavcopy import FIRST_ISIN_DAY
from .history_build import (
    ActionHooks,
    BuildPlan,
    BuildReport,
    BuildSteps,
    DbCatalog,
    run_history_build,
)
from .history_validation import Validation, mcp_close_source
from .index_history import (
    ArchiveIndexSource,
    IndexSource,
    NotFoundMemo,
    archive_fill_source,
    mcp_index_source,
)
from .mcp_client import McpClient
from .pacing import Pacer
from .run_report import save_report, stop_reason
from .store import DataStore

logger = logging.getLogger(__name__)

REPORT_DIR = Path("history_build")  # under the HAT data root: report-YYYY-MM-DD.txt per run day
INDEX_MEMO = Path("build") / "index_not_found.json"  # archive days NSE answered 404 for


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.history_cli", description=__doc__)
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_ISIN_DAY)
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    p.add_argument("--resume", action="store_true", help="skip days already downloaded or empty")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--seed", type=int, default=0, help="seed of the validation sample")
    p.add_argument(
        "--actions-archive", help="an NSE corporate-action CSV (SYMBOL, PURPOSE, EX-DATE)"
    )
    p.add_argument("--skip-actions", action="store_true")
    p.add_argument("--skip-index", action="store_true")
    p.add_argument("--skip-validation", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    end = args.end or date.today()
    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..models import HatCorporateAction, HatInstrument, HatMarket  # noqa: F401
    from . import calendar as hat_calendar
    from .bhavcopy import http_fetcher
    from .instruments import resolve_symbol, seed_symbol_history
    from .mcp_client import client_from_env
    from .renames import renames_from_bars

    logging.basicConfig(level=logging.INFO)
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatSessionLocal() as session:
        market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
        if market is None:
            sys.stderr.write("history_no_market\n")
            return 2
        market_id = market.id
        hat_calendar.store_recorded_history(session, market)
        session.commit()
        calendar = hat_calendar.session_calendar(session)
        holidays = calendar.holidays if calendar else frozenset()
        sessions = [d for d in calendar.trading_days() if d.weekday() >= 5] if calendar else []
    client = client_from_env(process_env())

    def instruments() -> tuple[dict[str, object], set[str]]:
        # read when the actions are loaded, so securities registered by the build are included;
        # a symbol several instruments share (an ISIN change keeps it) asks once, for the active
        # one (then the lowest id), and is listed as shared
        with HatSessionLocal() as session:
            rows = session.execute(
                select(HatInstrument.id, HatInstrument.symbol, HatInstrument.active).where(
                    HatInstrument.market_id == market_id
                )
            ).all()
        chosen: dict[str, object] = {}
        shared: set[str] = set()
        for instrument_id, symbol, _active in sorted(rows, key=lambda r: (not r[2], r[0].bytes)):
            if symbol in chosen:
                shared.add(symbol)
            chosen.setdefault(symbol, instrument_id)
        return chosen, shared

    def resolve(symbol: str, on: date) -> object | None:
        with HatSessionLocal() as session:
            found = resolve_symbol(session, market_id, symbol, on)
            return None if found is None else found.id

    def symbols() -> int:
        # the bars' own rename dates; the master would place a rename on the day it saw it
        with HatSessionLocal() as session:
            unmatched = seed_symbol_history(session, market_id, renames_from_bars(store))
            session.commit()
            return unmatched

    def sink(records: Sequence[ca.ActionRecord]) -> ca.LoadReport:
        with HatSessionLocal() as session:
            result = ca.load_actions(session, records)
            session.commit()
            return result

    fetch = http_fetcher()
    pacer = Pacer(
        min_interval=args.interval
    )  # one pacer: the bhavcopy and index files share a host
    index_source: IndexSource | None = None
    archive: ArchiveIndexSource | None = None
    if not args.skip_index:
        memo_path = settings.data_root / INDEX_MEMO
        memo = NotFoundMemo.load(memo_path) if args.resume else NotFoundMemo(memo_path)
        index_source = archive = ArchiveIndexSource(store, fetch, pacer, memo=memo)
        if client is not None:
            index_source = archive_fill_source(mcp_index_source(client), index_source)
    hooks = None
    if not args.skip_actions and (client is not None or args.actions_archive):
        sources = ActionSources(client, instruments, resolve, args.actions_archive)
        hooks = _action_hooks(sources, args.start, end, sink)
    holder: dict[str, BuildReport] = {}
    try:
        report = run_history_build(
            BuildPlan(
                store=store,
                fetch=fetch,
                catalog=DbCatalog(HatSessionLocal, market_id),
                start=args.start,
                end=end,
                holidays=holidays,
                resume=args.resume,
                weekend_sessions=sessions,
            ),
            BuildSteps(
                pacer=pacer,
                symbols=symbols,
                actions=hooks,
                index_source=index_source,
                index_absent=(lambda name: archive.absent[name.upper()]) if archive else None,
                validation=Validation(mcp_close_source(client), seed=args.seed)
                if client is not None and not args.skip_validation
                else None,
            ),
            holder,
        )
    except BaseException as exc:  # save what was done, then stop as before
        _save(settings.data_root, holder.get("report", BuildReport()), stop_reason(exc))
        raise
    _save(settings.data_root, report)
    return _exit_code(report)


def _save(root: Path, report: BuildReport, stopped: str | None = None) -> None:
    summary = _summary(report, stopped)
    sys.stdout.write(summary)
    save_report(root / REPORT_DIR, summary, datetime.now(IST))


def _exit_code(report: BuildReport) -> int:
    """Non-zero when anything needs the operator: a mismatch, an unplaced security, an unusable
    file, a day with no file that is not a holiday, an action lookup or an index that failed, an
    index day after the index's start with no row that its source does not explain, or an action
    whose factor conflicts with the stored one (the stored factor stays: a conflict is never a
    success)."""
    failed = report.validation is not None and report.validation.failed
    incomplete = report.unresolved or report.problems or report.days.gaps or report.days.unaccounted
    index_failed = (report.index is not None and report.index.failed) or any(
        report.index_missing.values()
    )
    actions = report.action_symbols_failed or (
        report.actions is not None and report.actions.conflicts
    )
    return 1 if failed or incomplete or actions or index_failed else 0


@dataclass(frozen=True)
class ActionSources:
    """Where the build's corporate actions come from: the MCP (None: not configured), the
    instruments to ask it for (symbol to instrument id, and the symbols several instruments
    share, read when the actions are loaded), the instrument a symbol named on an ex-date (the
    symbol history, for the archive and the shared symbols) and an operator-supplied NSE archive
    CSV."""

    client: McpClient | None
    instruments: Callable[[], tuple[Mapping[str, object], set[str]]]
    resolve: Callable[[str, date], object | None]
    archive: str | None = None


def _action_hooks(
    sources: ActionSources,
    start: date,
    end: date,
    sink: Callable[[Sequence[ca.ActionRecord]], ca.LoadReport],
) -> ActionHooks:
    """Actions from the MCP (first, `corporate_actions.lookup_actions`, the nightly lookup; an
    action of a shared symbol goes to the ISIN that held it on its ex-date, `by_ex_date`) and
    the archive CSV (fallback); the sink keeps the first value per key. A failed, partial or
    unreadable MCP page is counted; an archive row naming a split, bonus or consolidation
    without a usable ratio, or a symbol no instrument held on its ex-date, is reported
    (HAT-DATA-055), never dropped."""

    def loader() -> tuple[list[ca.ActionRecord], int, list[str]]:
        found = ca.ActionLookup()
        records: list[ca.ActionRecord] = []
        if sources.client is not None:
            chosen, shared = sources.instruments()
            found = ca.lookup_actions(sources.client, chosen, start, end)
            symbols = {iid: symbol for symbol, iid in chosen.items() if symbol in shared}
            records = by_ex_date(found.records, symbols, sources.resolve)
        unparsed: list[str] = []
        if sources.archive:
            text = Path(sources.archive).read_text(encoding="utf-8")
            parsed = ca.from_archive_csv(text, sources.resolve)
            records.extend(parsed.records)
            unparsed.extend(parsed.unparsed)
            for purpose in parsed.unparsed:
                logger.warning("archive action not parsed: %s", purpose)
        return records, len(found.failed), unparsed

    return ActionHooks(loader, sink)


def by_ex_date(
    records: Sequence[ca.ActionRecord],
    symbols: Mapping[object, str],
    resolve: Callable[[str, date], object | None],
) -> list[ca.ActionRecord]:
    """Each MCP record of an instrument whose symbol several instruments share (`symbols`: id to
    symbol) under the instrument that held the symbol on its ex-date, by (symbol, ISIN) as
    `ca_import` matches; a record the symbol history places nowhere keeps the asked one."""
    out = []
    for r in records:
        symbol = symbols.get(r.instrument_id)
        held = resolve(symbol, r.ex_date) if symbol is not None else None
        out.append(r if held is None or held == r.instrument_id else replace(r, instrument_id=held))
    return out


def _summary(report: BuildReport, stopped: str | None = None) -> str:
    lines = [
        f"downloaded {len(report.download.downloaded)}, already had "
        f"{len(report.download.already_have)}, no file {len(report.download.candidates)}, "
        f"errors {len(report.download.errors)}",
        f"rows written {report.rows_written} in {report.files_written} files",
        f"holiday candidates explained {len(report.days.holidays)}, "
        f"gaps (not on the holiday list) {len(report.days.gaps)}",
        f"unaccounted days {len(report.days.unaccounted)}, "
        f"action lookups failed or partial {report.action_symbols_failed}, "
        f"archive actions not parsed {len(report.actions_unparsed)}",
        f"unresolved securities {len(report.unresolved)}, problem days {len(report.problems)}",
        f"left out {dict(report.left_out)}",
    ]
    if report.renames_unmatched is not None:
        lines.append(f"symbol renames no instrument took: {report.renames_unmatched}")
    if report.actions is not None:
        lines.append(
            f"actions: created {report.actions.created}, unchanged {report.actions.unchanged}, "
            f"conflicts {len(report.actions.conflicts)}, owner-corrected keys kept "
            f"{report.actions.held}"
        )
    if report.index is not None:
        missing = {name: len(days) for name, days in report.index_missing.items() if days}
        absent = {name: len(days) for name, days in report.index_absent.items() if days}
        lines.append(
            f"index: {report.index.days_written} days, {report.index.points} rows, "
            f"failed {report.index.failed}, missing days {missing}, "
            f"not in the archive {absent}"
        )
    if report.validation is not None:
        lines.append(
            f"validation: {len(report.validation.samples)} samples, "
            f"{len(report.validation.mismatches)} mismatches, "
            f"{len(report.validation.unchecked)} unchecked, row counts for "
            f"{len(report.validation.row_counts)} days"
        )
    if stopped is not None:
        lines.append(f"stopped: {stopped} before the run finished (run again with --resume)")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
