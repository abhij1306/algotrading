"""The daily data update's step bodies. Each reuses the code path that already does its job: the
operator command lines run as child processes, and the nightly job bodies (`ingest.nightly`,
`quality.gate`, `ca.adjust`, `calendar.refresh`) run through `run_job` as the live scheduler runs
them.

NSE's website and archives are reached only with `HAT_NSE_WEB` on (`nse_web`). Off (the
default), the bars come from the bhavcopy MCP (`mcp_bars`), the index levels from the stored
archive file or the MCP's snapshot, and every file NSE publishes (the holiday and equity lists,
the PR archive, the symbol and name changes, the NIFTY 500 list, niftyindices' series) from the
owner's hand (`hand.py`): without it a step raises `StepManual`.

A body returns `Done` or raises `StepError` or `StepManual`; each names the session its data is
complete through. With `ctx.dry_run` a body works out what it would do and runs nothing."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - runs the repo's own command lines, never a shell
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app.paths import PROJECT_ROOT

from ...settings import process_env
from .plan import gaps, recent_sessions, sessions_to_fill

BACKEND = PROJECT_ROOT / "backend"
SCRIPTS = PROJECT_ROOT / "scripts" / "data"
WORK = Path("work")  # under the data root: the download tools' working directories
ADMIN = Path("admin")  # under the data root: the only folder the admin commands read files from


class StepError(Exception):
    def __init__(self, detail: str, last_good: date | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.last_good = last_good


class StepManual(Exception):  # noqa: N818 - an outcome, not an error: the owner's turn
    """The step needs a file NSE publishes that the owner has not handed over (`hand.manual`)."""

    def __init__(self, detail: str, last_good: date | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.last_good = last_good


@dataclass(frozen=True)
class Done:
    last_good: date | None
    detail: str


@dataclass(frozen=True)
class Context:
    root: Path  # the HAT data root
    sessions: Sequence[date]  # the stored trading days
    through: date  # the latest session to fill
    today: date  # IST
    log: Path  # this step's log file
    dry_run: bool
    duckdb_memory: str | None = None


# --- shared -----------------------------------------------------------------------------------


def child(ctx: Context, argv: Sequence[str], *, cwd: Path = BACKEND) -> int:
    """Run one command line, its output appended to the step's log; its exit code."""
    env = {**process_env(), "HAT_DATA_ROOT": str(ctx.root), "PYTHONUNBUFFERED": "1"}
    ctx.log.parent.mkdir(parents=True, exist_ok=True)
    with ctx.log.open("a") as out:
        out.write(f"$ {' '.join(argv)}\n")
        out.flush()
        return subprocess.run(  # noqa: S603 - argv built here from fixed parts
            list(argv), cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=env, check=False
        ).returncode


def module(name: str, *args: object) -> list[str]:
    return [sys.executable, "-m", name, *(str(a) for a in args)]


def script(name: str, *args: object, kotak: bool = False) -> list[str]:
    """A data script's command line; `kotak` for a call of Kotak Neo, run by `KOTAK_PYTHON` (the
    interpreter with neo-api-client and pyotp) when it is set."""
    python = (process_env().get("KOTAK_PYTHON") if kotak else None) or sys.executable
    return [python, str(SCRIPTS / name), *(str(a) for a in args)]


def chain(ctx: Context, commands: Sequence[Sequence[str]]) -> None:
    """Run the commands in order; `StepError` at the first that exits non-zero."""
    for argv in commands:
        code = child(ctx, argv)
        if code:
            raise StepError(f"{_name(argv)} exited {code}")


def _name(argv: Sequence[str]) -> str:
    """A command's short name for the status: the module or script, and its subcommand (the
    first argument that is neither an option nor a path)."""
    head, rest = (argv[2], argv[3:]) if argv[1] == "-m" else (Path(argv[1]).stem, argv[2:])
    word = next((a for a in rest if not a.startswith("-") and "/" not in a), "")
    return f"{head.rsplit('.', 1)[-1]} {word}".strip()


def would(commands: Sequence[Sequence[str]]) -> str:
    return "would run: " + "; ".join(_name(c) for c in commands)


def work_dir(ctx: Context, env: str, name: str) -> Path:
    """A download tool's working directory: its own environment variable, else under the data
    root's `work/`."""
    return Path(process_env().get(env) or ctx.root / WORK / name)


def web() -> bool:
    """`HAT_NSE_WEB` is on: the steps may ask NSE's website and archives."""
    from ..nse_web import enabled

    return enabled(process_env())


def _mcp():  # noqa: ANN202 - McpClient | None, imported late
    """The bhavcopy MCP client from the environment (`client_from_env`), None when it is `off`."""
    from ..mcp_client import client_from_env

    return client_from_env(process_env())


def _nse_text(ctx: Context, file, step: str, last_good: date | None):  # noqa: ANN001, ANN202
    """A TextSource of `file` (`hand.HandFile`): NSE's address with the web on, else the inbox's
    copy (and the paths to retire once read); `StepManual` when the owner has not given it."""
    from ..bhavcopy import http_fetcher
    from ..nse_files import url_source
    from . import hand

    if web():
        return url_source(file.url, http_fetcher()), []
    path = hand.find(ctx.root, file.name)
    if path is None:
        raise StepManual(hand.manual(step, [file]), last_good)
    return _file_text(path), [path]


def _file_text(path: Path) -> Callable[[], str]:
    return lambda: path.read_bytes().decode("utf-8-sig")


def _retire(ctx: Context, paths: Sequence[Path]) -> None:
    from . import hand

    if paths and not ctx.dry_run:
        hand.retire(ctx.root, paths, ctx.today)


def _store(ctx: Context):  # noqa: ANN202 - DataStore, imported late with the database
    from ..store import DataStore

    return DataStore(ctx.root, memory_limit=ctx.duckdb_memory)


def _last(days: Sequence[date], through: date | None = None) -> date | None:
    return max((d for d in days if through is None or d <= through), default=None)


def _last_job(job: str) -> date | None:
    """The latest trade date `job` succeeded for (`hat_job_run`)."""
    from app.database import HatDataSessionLocal

    from ...models import HatJobRun

    with HatDataSessionLocal() as session:
        return session.scalar(
            select(func.max(HatJobRun.trade_date)).where(
                HatJobRun.job == job, HatJobRun.status == "succeeded"
            )
        )


def _each_session(
    ctx: Context, job: str, days: Sequence[date], body: Callable[[date], Callable[[], object]]
) -> Done:
    """Run `job` through `run_job` for each day, oldest first, stopping at the first failure (a
    later day waits for it, as the live chain does)."""
    from app.database import HatDataSessionLocal

    from ...ops.jobs import RunStatus, run_job

    if ctx.dry_run:
        return Done(_last_job(job), f"would run {job} for {_days(days)}")
    ran = []
    for day in days:
        outcome = run_job(job, day, body(day), session_factory=HatDataSessionLocal)
        if outcome.status != RunStatus.SUCCEEDED:
            raise StepError(f"{job} {day}: {outcome.error or outcome.status}", _last_job(job))
        ran.append(day)
    return Done(_last_job(job), f"{job} for {_days(ran)}")


def _days(days: Sequence[date]) -> str:
    if not days:
        return "no session"
    return str(days[0]) if len(days) == 1 else f"{len(days)} sessions {days[0]}..{days[-1]}"


# --- the steps --------------------------------------------------------------------------------


def calendar(ctx: Context) -> Done:
    """`calendar.refresh` from NSE's holiday list (weekly: the list changes a few times a
    year)."""
    from app.database import HatDataSessionLocal

    from ...daily.nightly import CALENDAR_REFRESH
    from ..reference_jobs import calendar_refresh
    from . import hand

    source, used = _nse_text(ctx, hand.HOLIDAYS, "calendar", _last(ctx.sessions))

    def body(_day: date) -> Callable[[], object]:
        return calendar_refresh(holiday_list=source, session_factory=HatDataSessionLocal)

    done = _each_session(ctx, CALENDAR_REFRESH, [ctx.today], body)
    _retire(ctx, used)
    return Done(_last(ctx.sessions), done.detail)


def listing(ctx: Context) -> Done:
    """`listing.refresh`: the stocks of the instrument master from NSE's equity list, after the
    bars step registered the day's new ISINs (inactive until a listing names them)."""
    from app.database import HatDataSessionLocal

    from ..reference_jobs import LISTING_REFRESH, listing_refresh
    from . import hand

    last = None if ctx.dry_run else _last_job(LISTING_REFRESH)
    source, used = _nse_text(ctx, hand.EQUITY_LIST, "listing", last)

    def body(day: date) -> Callable[[], object]:
        return listing_refresh(equity_list=source, session_factory=HatDataSessionLocal, today=day)

    done = _each_session(ctx, LISTING_REFRESH, [ctx.today], body)
    _retire(ctx, used)
    return done


def bars(ctx: Context) -> Done:
    from ..store import DATASET_BARS

    last = _last(_store(ctx).days(DATASET_BARS))
    todo = sessions_to_fill(ctx.sessions, last, ctx.through)
    if not todo:
        return Done(last, "up to date")
    if web():
        tool = "history_cli"
        argv = module(
            "app.hat.data.history_cli", "--from", todo[0], "--to", ctx.through, "--resume"
        )
    else:
        tool = "mcp_bars"  # the bhavcopy MCP's rows, as the archive file would give them
        argv = module("app.hat.data.mcp_bars", "--from", todo[0], "--to", ctx.through)
    if ctx.dry_run:
        return Done(last, f"{would([argv])} for {_days(todo)}")
    code = child(ctx, argv)
    now = _last(_store(ctx).days(DATASET_BARS))
    if code or now is None or now < ctx.through:
        raise StepError(f"{tool} exited {code}, bars through {now}", now)
    return Done(now, f"{tool} for {_days(todo)}")


def ingest(ctx: Context) -> Done:  # noqa: C901, PLR0915 - one loop, two source sets
    """The nightly ingest over days whose bars are stored: it reuses the bars partition and
    stores the index levels (the stored index archive file; with the web on NSE's archive, else
    the bhavcopy MCP's snapshot), the market breadth (counted from the bars), the NIFTY 500 TRI
    and `ingest.completed`, which the quality gate reads. The TRI comes from niftyindices with
    the web on, else from its answers the owner handed over (kept under `raw_tri/`); the days
    still without one are the `tri` step's."""
    from ...clock import SystemClock
    from ...daily.nightly import INGEST_NIGHTLY, ingest_outcome_recorder
    from ...ops.jobs import run_job
    from .. import ingest as nightly_ingest
    from ..bhavcopy import http_fetcher
    from ..index_history import (
        NIFTY_INDICES_INTERVAL,
        http_poster,
        kept_tri_source,
        tri_source,
    )
    from ..mcp_client import offline_client
    from ..pacing import Pacer
    from ..store import DATASET_BARS

    store = _store(ctx)
    stored = set(store.days(DATASET_BARS))
    todo = [
        d
        for d in sessions_to_fill(ctx.sessions, _last_job(INGEST_NIGHTLY), ctx.through)
        if d in stored
    ]
    on = web()
    if ctx.dry_run or not todo:
        return _each_session(ctx, INGEST_NIGHTLY, todo, lambda _d: lambda: None)
    from app.database import HatDataSessionLocal

    from ...ops.jobs import RunStatus

    fetch, clock = (http_fetcher() if on else None), SystemClock()
    if on:
        tri = tri_source(http_poster(), Pacer(min_interval=NIFTY_INDICES_INTERVAL))
    else:
        tri = kept_tri_source(ctx.root / TRI_RAW)
    client = _mcp() if not on else None
    lines = []
    for day in todo:
        holder: dict[str, nightly_ingest.IngestReport] = {}
        deps = nightly_ingest.IngestDeps(
            client=client or offline_client(),
            store=store,
            session_factory=HatDataSessionLocal,
            clock=clock,
            fallback=(lambda d: nightly_ingest.bhavcopy_day(store, fetch, d)) if fetch else None,
            index_csv=index_csv(store, fetch),
            tri=tri,
        )
        outcome = run_job(
            INGEST_NIGHTLY,
            day,
            nightly_ingest.ingest_body(deps, day, holder),
            session_factory=HatDataSessionLocal,
            on_finish=ingest_outcome_recorder(holder, day, clock),
        )
        if outcome.status != RunStatus.SUCCEEDED:
            raise StepError(f"{INGEST_NIGHTLY} {day}: {outcome.error}", _last_job(INGEST_NIGHTLY))
        report = holder.get("report")
        if report is not None:
            lines.append(
                f"{day} bars {report.stored} index {report.index_rows} breadth"
                f" {report.breadth_source} tri +{report.tri_stored}"
                + (f" tri_missing {len(report.tri_missing)}" if report.tri_missing else "")
            )
    detail = "; ".join(lines) or f"{_days(todo)} already done"
    return Done(_last_job(INGEST_NIGHTLY), detail)


def tri(ctx: Context) -> Done:
    """With the web off, the NIFTY 500 TRI of every bar day since the last stored one (however
    long ago: a file handed over late still fills it), from the files the owner handed over
    (kept under `raw_tri/`); a day still without one reports `manual`. With the web on the
    nightly ingest fetches it."""
    from ...daily.nightly import INGEST_NIGHTLY
    from ..index_history import (
        NIFTY_500_TRI,
        kept_tri_source,
        last_index_day,
        nightly_tri,
        tri_since,
    )

    if web():
        return Done(_last_job(INGEST_NIGHTLY), "with the nightly ingest (web on)")
    store = _store(ctx)
    last = last_index_day(store, NIFTY_500_TRI, ctx.through)
    since = tri_since(last, ctx.through)
    if ctx.dry_run:
        return Done(last, f"would read the handed TRI files for the bar days from {since}")
    keep_tri_files(ctx)
    source = kept_tri_source(ctx.root / TRI_RAW)
    report = nightly_tri(store, source, ctx.through, apply=True, since=since)
    left = report.left.get(NIFTY_500_TRI, [])
    stored = len(report.found.get(NIFTY_500_TRI, []))
    last = last_index_day(store, NIFTY_500_TRI, ctx.through)
    if left:
        raise StepManual(f"+{stored}; {tri_manual(left, ctx.through)}", last)
    return Done(last, f"TRI through {last}, +{stored}")


TRI_RAW = Path("raw_tri")  # under the data root: `index_history.TRI_RAW_DIR`


def keep_tri_files(ctx: Context) -> None:
    """Keep the NIFTY 500 TRI answers the owner handed over under `raw_tri/`, create-only (a
    kept file is never rewritten; a different one under a kept name is logged and left)."""
    import logging

    from ..index_history import NIFTY_500_TRI, parse_tri_json
    from ..store import StoreError, keep_once
    from . import hand

    for path in hand.matching(ctx.root, NIFTY_500_TRI.replace(" ", "_") + "_*.json"):
        data = path.read_bytes()
        if parse_tri_json(data, NIFTY_500_TRI) is None:
            logging.getLogger(__name__).warning("%s is no TRI answer; not kept", path.name)
            continue
        try:
            keep_once(ctx.root / TRI_RAW / path.name, data)
        except StoreError:
            logging.getLogger(__name__).warning("%s differs from the kept file", path.name)


def tri_manual(left: Sequence[date], through: date) -> str:
    """The `manual` detail for the TRI days still missing: one file per calendar year."""
    from ..index_history import NIFTY_500_TRI, tri_raw_range
    from . import hand

    files = []
    for year in sorted({d.year for d in left}):
        span = tri_raw_range(NIFTY_500_TRI, year, through)
        if span is not None:
            files.append(hand.nifty500_tri(*span))
    return hand.manual("ingest", files, f"NIFTY 500 TRI missing for {_days(sorted(left))}")


def index_csv(store, fetch) -> Callable[[date], str | None]:  # noqa: ANN001
    """A day's index archive CSV: the copy the history build kept, else (with a fetcher: the
    web on) NSE's archive, else its niftyindices mirror (whose page for a missing file parses to
    no rows); None sends the ingest to the bhavcopy MCP's snapshot."""
    from ..index_history import INDEX_ARCHIVE_URL, INDEX_MIRROR_URL, index_archive_name

    def read(day: date) -> str | None:
        name = index_archive_name(day)
        if store.raw_exists(name):
            return store.read_raw(name).decode("utf-8-sig", errors="replace")
        if fetch is None:
            return None
        for base in (INDEX_ARCHIVE_URL, INDEX_MIRROR_URL):
            result = fetch(base + name)
            if result.status == "ok" and result.data:
                return result.data.decode("utf-8-sig", errors="replace")
        return None

    return read


def quality(ctx: Context) -> Done:
    from functools import partial

    from app.database import HatDataSessionLocal

    from ...clock import SystemClock
    from ...daily.nightly import INGEST_NIGHTLY, quality_verdict
    from ..quality import JOB_QUALITY, gate_body

    store, ingested = _store(ctx), _last_job(INGEST_NIGHTLY)
    days = [
        d
        for d in sessions_to_fill(ctx.sessions, _last_job(JOB_QUALITY), ctx.through)
        if ingested is not None and d <= ingested
    ]
    body = partial(_gate, gate_body, HatDataSessionLocal, store, quality_verdict, SystemClock())
    return _each_session(ctx, JOB_QUALITY, days, body)


def _gate(gate_body, factory, store, verdict, clock, day: date):  # noqa: ANN001, ANN202
    return gate_body(
        session_factory=factory, store=store, trade_date=day, verdict=verdict, clock=clock
    )


def ca(ctx: Context) -> Done:
    """`ca.adjust` for each ingested session: the day's `bc` file (NSE's PR archive), its
    dividends and demergers, the lineage refresh (NSE's symbol and name changes) and, with the
    bhavcopy MCP configured, the lookups of the next session's actions (else alerted
    `corporate_action.unchecked`, to the log here). With the web off a session runs only once
    the owner handed over its PR archive (`manual` names the rest, the later sessions waiting
    for the first); the change lists are read when handed over, else the stored lineage
    stands."""
    from ...daily.nightly import CA_ADJUST, INGEST_NIGHTLY
    from ...daily.nightly_ca import CaAdjust
    from . import hand

    ingested = _last_job(INGEST_NIGHTLY)
    days = [
        d
        for d in sessions_to_fill(ctx.sessions, _last_job(CA_ADJUST), ctx.through)
        if ingested is not None and d <= ingested
    ]
    if web():
        deps = _nightly_deps(ctx, _web_sources())
        return _each_session(ctx, CA_ADJUST, days, lambda day: lambda: CaAdjust(deps).run(day))
    archives = {d: hand.find(ctx.root, hand.pr_archive(d).name) for d in days}
    ready = ready_prefix(days, {d for d, path in archives.items() if path is not None})
    changes = [hand.find(ctx.root, f.name) for f in (hand.SYMBOL_CHANGES, hand.NAME_CHANGES)]
    sources = _hand_sources(archives, *changes)
    deps = _nightly_deps(ctx, sources)
    done = _each_session(ctx, CA_ADJUST, ready, lambda day: lambda: CaAdjust(deps).run(day))
    _retire(ctx, [p for p in (*changes, *(archives[d] for d in ready)) if p is not None])
    waiting = days[len(ready) :]
    if waiting:
        files = [hand.pr_archive(d) for d in waiting]
        note = f"{done.detail}; the PR archive of {_days(waiting)} (with {hand.SYMBOL_CHANGES.name}"
        note += f" and {hand.NAME_CHANGES.name} for the ISIN lineage: {hand.SYMBOL_CHANGES.url},"
        note += f" {hand.NAME_CHANGES.url})"
        raise StepManual(hand.manual("ca", files, note), done.last_good)
    return done


def ready_prefix(days: Sequence[date], have: set[date]) -> list[date]:
    """The sessions, oldest first, up to the first one without its file: a later session waits
    for an earlier one, as the live chain does."""
    out = []
    for day in days:
        if day not in have:
            break
        out.append(day)
    return out


def _web_sources():  # noqa: ANN202 - TextSources, imported late
    """The NSE files `ca.adjust` reads, downloaded (the web on)."""
    from ...daily.nightly_deps import TextSources
    from ..bhavcopy import http_fetcher
    from ..nse_changes import NAME_CHANGES_URL, SYMBOL_CHANGES_URL
    from ..nse_files import EQUITY_LIST_URL, HOLIDAYS_URL, corporate_action_file, url_source

    fetch = http_fetcher()
    return TextSources(
        equity_list=url_source(EQUITY_LIST_URL, fetch),
        holidays=url_source(HOLIDAYS_URL, fetch),
        price_bands=_unused,
        symbol_changes=url_source(SYMBOL_CHANGES_URL, fetch),
        name_changes=url_source(NAME_CHANGES_URL, fetch),
        corporate_actions=lambda day: corporate_action_file(day, fetch),
    )


def _hand_sources(archives, symbol_changes, name_changes):  # noqa: ANN001, ANN202
    """The NSE files `ca.adjust` reads, from the owner's hand (the web off): a session's `bc`
    CSV out of its PR archive, and the change lists only when both were given."""
    from ...daily.nightly_deps import TextSources
    from ..nse_files import pr_member

    def bc(day: date) -> Callable[[], str]:
        path = archives.get(day)
        if path is None:
            return lambda: _unused()  # never asked: only a ready session runs
        return lambda: pr_member(path.read_bytes(), f"bc{day:%d%m%Y}.csv")

    both = symbol_changes is not None and name_changes is not None
    return TextSources(
        equity_list=_unused,
        holidays=_unused,
        price_bands=_unused,
        symbol_changes=_file_text(symbol_changes) if both else None,
        name_changes=_file_text(name_changes) if both else None,
        corporate_actions=bc,
    )


def _unused(*_args: object) -> str:
    raise RuntimeError("not a data job's input")


def _nightly_deps(ctx: Context, sources):  # noqa: ANN001, ANN202 - NightlyDeps for data jobs
    """The `NightlyDeps` `ca.adjust` reads (store, data sessions, clock, the bhavcopy MCP, the
    NSE text files); the trading fields it never reads are inert."""
    from app.database import HatDataSessionLocal

    from ...clock import SystemClock
    from ...daily.nightly_deps import NightlyDeps

    def no_fetch() -> object:
        raise RuntimeError("not a data job's input")

    return NightlyDeps(
        store=_store(ctx),
        session_factory=HatDataSessionLocal,
        data_session_factory=HatDataSessionLocal,
        clock=SystemClock(),
        user_id=uuid.UUID(int=0),
        mcp=_mcp,
        bhavcopy=no_fetch,  # type: ignore[arg-type]
        sources=sources,
        total_equity=lambda _day: Decimal(0),
        fills_read_at=lambda _account: None,
        sleep=lambda _seconds: None,
    )


def features(ctx: Context) -> Done:
    from ..store import DATASET_FEATURES

    argv = module("app.hat.data.features", "--through", ctx.through)
    if ctx.dry_run:
        return Done(_last(_store(ctx).days(DATASET_FEATURES)), would([argv]))
    code = child(ctx, argv)
    last = _last(_store(ctx).days(DATASET_FEATURES))
    if code or last is None or last < ctx.through:
        raise StepError(f"features exited {code}, features through {last}", last)
    return Done(last, f"features through {last}")


def membership(ctx: Context) -> Done:
    """Rebuild the NIFTY 500 history from the notices and today's list (only new notices and
    texts are downloaded), then load it as of today. `build` refuses a notice symbol it cannot
    resolve: that needs the hand-kept `overrides.json` / `symbol_alias.json` (runbook). With the
    web off nothing is downloaded: the list is the one the owner handed over with the day it was
    downloaded (`--as-of`; with any new notice PDF, `*.pdf` in the inbox), built and loaded as of
    that day, never before the latest stored change in effect; else the step is `manual` and
    the loaded history stands (covered to the day of its list)."""
    from . import hand

    last = _covered_to()
    if last is not None and last >= ctx.through:
        return Done(last, "up to date")
    work = work_dir(ctx, "NIFTY500_DIR", "nifty500")
    tool = "nifty500_membership.py"
    steps = ("fetch", "text", "parse", "reference") if web() else ("text", "parse", "reference")
    listed, notices, as_of = None, [], ctx.today
    if not web():
        listed = hand.find(ctx.root, hand.NIFTY500_LIST.name)
        notices = hand.matching(ctx.root, "*.pdf")
        note = f"NIFTY 500 covered to {last}; any new NIFTY 500 notice PDF from {hand.NIFTY_PRESS}"
        given = hand.as_of(listed) if listed is not None else None
        if listed is None or given is None:
            if listed is not None:
                note = f"{hand.NIFTY500_LIST.name} was handed over without --as-of; {note}"
            detail = hand.manual("membership", [hand.NIFTY500_LIST], note, dated=True)
            raise StepManual(detail, last)
        refused = hand.as_of_refused(given, ctx.today, _latest_member_change(ctx.today))
        if refused is not None:
            raise StepError(refused, last)
        as_of = given
    build = [
        *(script(tool, "--work", work, step) for step in steps),
        script(tool, "--work", work, "build", "--as-of", as_of),
    ]
    load = membership_load(as_of)
    if ctx.dry_run:
        return Done(last, would([*build, load]))
    if listed is not None:  # where `fetch` would have put them
        (work / "raw" / "press").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(listed, work / "raw" / hand.NIFTY500_LIST.name)
        for pdf in notices:
            shutil.copyfile(pdf, work / "raw" / "press" / pdf.name)
    try:
        chain(ctx, build)
        # the admin command reads only files inside the admin folder: the build is copied there
        (ctx.root / ADMIN).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            work / "nifty500-membership.csv", ctx.root / ADMIN / membership_admin_name(as_of)
        )
        chain(ctx, [load])
    except StepError as exc:
        raise StepError(exc.detail, _covered_to()) from None
    _retire(ctx, [p for p in (listed, *notices) if p is not None])
    return Done(_covered_to(), f"loaded as of {as_of}")


def _latest_member_change(today: date) -> date | None:
    """The latest NIFTY 500 entry or exit stored that is in effect by `today`."""
    from sqlalchemy import func

    from app.database import HatDataSessionLocal

    from ...models import HatIndexMember

    member = HatIndexMember
    with HatDataSessionLocal() as session:
        starts = session.scalar(
            select(func.max(member.effective_from)).where(
                member.index_name == "NIFTY 500", member.effective_from <= today
            )
        )
        ends = session.scalar(
            select(func.max(member.effective_to)).where(
                member.index_name == "NIFTY 500", member.effective_to <= today
            )
        )
    return max((d for d in (starts, ends) if d is not None), default=None)


def membership_admin_name(today: date) -> str:
    """The name, inside the admin folder, of the NIFTY 500 history built on `today`."""
    return f"nifty500-membership-{today.isoformat()}.csv"


def membership_load(today: date) -> list[str]:
    """`load-index-membership` of the history built on `today`, named inside the admin folder
    (a path outside it is refused `file_outside_admin`)."""
    return module(
        "app.hat.admin",
        "load-index-membership",
        "--csv",
        membership_admin_name(today),
        "--as-of",
        today,
        "--apply",
    )


def _covered_to() -> date | None:
    from app.database import HatDataSessionLocal

    from ...models import HatIndexMembership

    with HatDataSessionLocal() as session:
        return session.scalar(
            select(HatIndexMembership.covered_to).where(
                HatIndexMembership.index_name == "NIFTY 500"
            )
        )


LOOKBACK_5M = 10  # sessions checked per member every run: one Kotak call per member covers 30 days


def bars5m(ctx: Context) -> Done:
    """Kotak's 5-minute bars of today's members (`kotak_5m.py members`, then `topup`) from the
    first session in which a member with a daily bar has no 5-minute bars and no ledgered
    refusal, checked per instrument over the last `LOOKBACK_5M` sessions and every one after
    the last stored day; then `bars5m_import`, which adds only the days not stored and holds the
    minute-history lock (a running history job fails this step; the next run retries). Fails
    while any such member-day is still missing."""
    from ..bars_store import BarStore

    work = work_dir(ctx, "KOTAK_5M_DIR", "kotak_5m")
    tool = "kotak_5m.py"
    members = script(tool, "--work", work, "members")
    if not ctx.dry_run:
        chain(ctx, [members])
    store = BarStore(ctx.root)
    window = recent_sessions(ctx.sessions, _last(store.days()), ctx.through, LOOKBACK_5M)
    before = _gaps_5m(ctx, store, work, window)
    if before is None:
        if ctx.dry_run:
            return Done(None, f"{would([members])}, then a per-member check of {_days(window)}")
        raise StepError("kotak_5m members wrote no universe_raw.csv")
    if not before:
        return Done(ctx.through, f"every member's bars stored for {_days(window)}")
    start = min(before)
    commands = [
        script(tool, "--work", work, "topup", "--from", start, "--to", ctx.through, kotak=True),
        module("app.hat.data.bars5m_import", "--source", work / "raw_5m_topup", "--apply"),
    ]
    if ctx.dry_run:
        return Done(None, f"{would([members, *commands])} from {start}: {_count(before)}")
    fetched = child(ctx, commands[0])  # import what was fetched even when some members failed
    imported = child(ctx, commands[1])
    after = _gaps_5m(ctx, store, work, window) or {}
    good = min(after) if after else None
    last_good = max((d for d in window if good is None or d < good), default=None)
    if fetched or imported or after:
        raise StepError(
            f"topup exited {fetched}, import exited {imported}; still missing {_count(after)}",
            last_good,
        )
    return Done(last_good, f"5-minute bars from {start}: {_count(before)} filled")


def _count(gaps: dict[date, set[str]]) -> str:
    if not gaps:
        return "none"
    first = min(gaps)
    return f"{sum(len(v) for v in gaps.values())} member-days, first {first} ({len(gaps[first])})"


def _gaps_5m(
    ctx: Context,
    store,  # noqa: ANN001 - BarStore, imported late
    work: Path,
    window: Sequence[date],
) -> dict[date, set[str]] | None:  # noqa: ANN001
    """Per session of `window`, today's members (`universe_raw.csv`) with a daily bar and no
    5-minute bars nor a ledgered refusal of that day; None without the member list."""
    import csv

    import pyarrow.parquet as pq

    from ..store import DATASET_BARS

    universe = work / "universe_raw.csv"
    if not universe.exists():
        return None
    with universe.open(newline="") as f:
        members = {row[3] for row in csv.reader(f) if len(row) > 3}
    daily = _store(ctx)
    expected = {}
    for day in window:
        traded = set()
        for path in daily.partition_files(DATASET_BARS, day):
            traded |= {
                str(i) for i in pq.read_table(path, columns=["instrument_id"])[0].to_pylist()
            }
        expected[day] = members & traded
    explained = {(r.instrument_id, r.day) for r in store.read_rejects() if r.day in expected}
    return gaps(expected, {d: store.instruments(d) for d in window}, explained)


# The overlay's niftyindices series (`overlay_etf.py`'s `TRI_INDICES` and `RATE_INDEX`): the
# folder under `<work>/niftyindices/`, and the index a request names (`hand.niftyindices_series`).
OVERLAY_SERIES = {
    "NIFTY_50_TRI": ("NIFTY 50", "tri"),
    "NIFTY_500_TRI": ("NIFTY 500", "tri"),
    "NIFTY_1D_RATE_INDEX": ("NIFTY 1D RATE INDEX", "history"),
}


def etf(ctx: Context) -> Done:
    """The overlay's raw files ask only for what is missing (the current windows), then `build`
    and `store` rewrite `HAT_DATA_ROOT/etf/`. `local` copies HAT's own files once. With the web
    off `indices` asks nothing: the niftyindices answers the owner handed over go into its
    folders, and `build` and `store` run only once each series reaches the session (else
    `manual`, the stored overlay standing)."""
    work = work_dir(ctx, "OVERLAY_DATA_DIR", "overlay")
    on = web()
    steps = ("local", "kotak", "indices", "dividends") if on else ("local", "kotak", "dividends")
    fetches = [script("overlay_etf.py", "--work", work, s, kotak=s == "kotak") for s in steps]
    builds = [script("overlay_etf.py", "--work", work, s) for s in ("build", "store")]
    if not on and not ctx.dry_run:
        _overlay_hand_files(ctx, work)
    short = [] if on else overlay_short(work, ctx.through)
    if ctx.dry_run:
        return Done(None, would([*fetches, *([] if short else builds)]))
    chain(ctx, fetches)
    if short:
        raise StepManual(overlay_manual(short, ctx.through), None)
    chain(ctx, builds)
    return Done(ctx.through, "built and stored")


def _overlay_hand_files(ctx: Context, work: Path) -> None:
    """Copy each niftyindices answer the owner handed over (`<SERIES>_<from>_<to>.json`) into the
    overlay's folder as `<from>_<to>.json` when it parses to a series (`overlay_days`), never over
    a valid file there (a raw file is kept once) but over a broken one."""
    import logging

    from . import hand

    for folder in OVERLAY_SERIES:
        for path in hand.matching(ctx.root, f"{folder}_*.json"):
            if hand.series_end(path.name, folder) is None:
                continue
            data = path.read_bytes()
            target = work / "niftyindices" / folder / path.name.removeprefix(folder + "_")
            if not overlay_days(data, folder):
                logging.getLogger(__name__).warning(
                    "%s is no %s series; not used", path.name, folder
                )
                continue
            if target.exists() and overlay_days(target.read_bytes(), folder):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


# the overlay's keys per kind (`overlay_etf.py` `_index_files`): the day, then the level
OVERLAY_KEYS = {"tri": ("Date", "TotalReturnsIndex"), "history": ("HistoricalDate", "CLOSE")}


def overlay_days(data: bytes, folder: str) -> list[date]:
    """The days of a niftyindices answer for the overlay series `folder` that carry a positive
    level, as the overlay's build reads them, oldest first; none for an answer that is no JSON
    list (a login or error page) or a list without one such row."""
    import json
    from datetime import datetime

    date_key, value_key = OVERLAY_KEYS[OVERLAY_SERIES[folder][1]]
    try:
        rows = json.loads(data.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return []
    out = set()
    for row in rows if isinstance(rows, list) else []:
        try:
            day = datetime.strptime(str(row.get(date_key, "")).strip(), "%d %b %Y").date()
            value = float(str(row.get(value_key, "")).replace(",", ""))
        except (AttributeError, ValueError):
            continue
        if value > 0:
            out.add(day)
    return sorted(out)


def overlay_short(work: Path, through: date) -> list[str]:
    """The overlay's niftyindices series whose kept files hold no day on or after `through` (read
    from the files, not their names)."""
    short = []
    for folder in OVERLAY_SERIES:
        last = [
            days[-1]
            for p in (work / "niftyindices" / folder).glob("*.json")
            if (days := overlay_days(p.read_bytes(), folder))
        ]
        if not last or max(last) < through:
            short.append(folder)
    return short


def overlay_manual(short: Sequence[str], through: date) -> str:
    """The `manual` detail for the overlay series that do not reach `through`: this year's
    answer of each, from 1 January to `through`."""
    from ..index_history import HISTORY_URL, TRI_URL
    from . import hand

    start = date(through.year, 1, 1)
    files = []
    for folder in short:
        base, kind = OVERLAY_SERIES[folder]
        url = TRI_URL if kind == "tri" else HISTORY_URL
        files.append(hand.niftyindices_series(folder.replace("_", " "), base, url, start, through))
    return hand.manual("etf", files, f"overlay series short of {through}: {', '.join(short)}")


def results(ctx: Context) -> Done:
    """NSE's filings (listings, announcements, XBRL), the secondary source's pages, parse and
    derive. With the web off NSE's are not fetched and no hand import exists for them (one
    listing per company and one XBRL per filing): the quarters NSE's cache lacks come from the
    secondary source, which counts from its fetch (late, never early), and the detail says so."""
    on = web()
    steps = ("fetch", "fetch-secondary", "parse", "derive") if on else RESULTS_WEB_OFF
    commands = [module("app.hat.data.results", s) for s in steps]
    if ctx.dry_run:
        return Done(None, would(commands))
    chain(ctx, commands)
    if on:
        return Done(ctx.through, "fetched, parsed, derived")
    return Done(ctx.through, "nse web off: secondary source fetched, parsed, derived")


RESULTS_WEB_OFF = ("fetch-secondary", "parse", "derive")


def fundamentals(ctx: Context) -> Done:
    commands = [module("app.hat.data.fundamentals", s) for s in ("extract", "derive")]
    if ctx.dry_run:
        return Done(None, would(commands))
    chain(ctx, commands)
    return Done(ctx.through, "extracted, derived")


def record(ctx: Context) -> Done:
    """The stores' manifests and the reference CSVs into the data repo clone, committed and
    pushed when they changed (`record.refresh`)."""
    from ...settings import load_settings
    from .. import record as data_record

    clone = load_settings().data_repo
    if ctx.dry_run:
        return Done(None, f"would refresh the manifests and reference CSVs in {clone}")
    try:
        detail = data_record.refresh(ctx.root, clone)
    except data_record.RecordError as exc:
        raise StepError(str(exc)) from exc
    return Done(ctx.through, detail)


BODIES: dict[str, Callable[[Context], Done]] = {
    "calendar": calendar,
    "bars": bars,
    "listing": listing,
    "ingest": ingest,
    "tri": tri,
    "quality": quality,
    "ca": ca,
    "features": features,
    "membership": membership,
    "bars5m": bars5m,
    "etf": etf,
    "results": results,
    "fundamentals": fundamentals,
    "record": record,
}
