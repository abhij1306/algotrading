"""The daily data update's step bodies. Each reuses the code path that already does its job: the
operator command lines run as child processes, and the nightly job bodies (`ingest.nightly`,
`quality.gate`, `ca.adjust`, `calendar.refresh`) run through `run_job` as the live scheduler runs
them, without the NSE MCP servers (the bhavcopy, NSE's index archive and niftyindices instead).

A body returns `Done` or raises `StepError`; either names the session its data is complete
through. With `ctx.dry_run` a body works out what it would do and runs nothing."""

from __future__ import annotations

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


class StepError(Exception):
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
    from app.database import HatDataSessionLocal

    from ...daily.nightly import CALENDAR_REFRESH
    from ..bhavcopy import http_fetcher
    from ..nse_files import HOLIDAYS_URL, url_source
    from ..reference_jobs import calendar_refresh

    def body(_day: date) -> Callable[[], object]:
        return calendar_refresh(
            holiday_list=url_source(HOLIDAYS_URL, http_fetcher()),
            session_factory=HatDataSessionLocal,
        )

    done = _each_session(ctx, CALENDAR_REFRESH, [ctx.today], body)
    return Done(_last(ctx.sessions), done.detail)


def bars(ctx: Context) -> Done:
    from ..store import DATASET_BARS

    last = _last(_store(ctx).days(DATASET_BARS))
    todo = sessions_to_fill(ctx.sessions, last, ctx.through)
    if not todo:
        return Done(last, "up to date")
    argv = module("app.hat.data.history_cli", "--from", todo[0], "--to", ctx.through, "--resume")
    if ctx.dry_run:
        return Done(last, f"{would([argv])} for {_days(todo)}")
    code = child(ctx, argv)
    now = _last(_store(ctx).days(DATASET_BARS))
    if code or now is None or now < ctx.through:
        raise StepError(f"history_cli exited {code}, bars through {now}", now)
    return Done(now, f"bhavcopy for {_days(todo)}")


def ingest(ctx: Context) -> Done:
    """The nightly ingest over days whose bars are stored: it reuses the bars partition and
    stores the index levels (NSE's index archive), the market breadth (counted from the bars),
    the NIFTY 500 TRI (niftyindices) and `ingest.completed`, which the quality gate reads."""
    from ...clock import SystemClock
    from ...daily.nightly import INGEST_NIGHTLY, ingest_outcome_recorder
    from ...ops.jobs import run_job
    from .. import ingest as nightly_ingest
    from ..bhavcopy import http_fetcher
    from ..index_history import NIFTY_INDICES_INTERVAL, http_poster, tri_source
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
    if ctx.dry_run or not todo:
        return _each_session(ctx, INGEST_NIGHTLY, todo, lambda _d: lambda: None)
    from app.database import HatDataSessionLocal

    from ...ops.jobs import RunStatus

    fetch, clock = http_fetcher(), SystemClock()
    tri = tri_source(http_poster(), Pacer(min_interval=NIFTY_INDICES_INTERVAL))
    lines = []
    for day in todo:
        holder: dict[str, nightly_ingest.IngestReport] = {}
        deps = nightly_ingest.IngestDeps(
            client=offline_client(),
            store=store,
            session_factory=HatDataSessionLocal,
            clock=clock,
            fallback=lambda d: nightly_ingest.bhavcopy_day(store, fetch, d),
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
    return Done(_last_job(INGEST_NIGHTLY), "; ".join(lines) or f"{_days(todo)} already done")


def index_csv(store, fetch) -> Callable[[date], str | None]:  # noqa: ANN001
    """A day's index archive CSV: the copy the history build kept, else NSE's archive, else its
    niftyindices mirror (whose page for a missing file parses to no rows)."""
    from ..index_history import INDEX_ARCHIVE_URL, INDEX_MIRROR_URL, index_archive_name

    def read(day: date) -> str | None:
        name = index_archive_name(day)
        if store.raw_exists(name):
            return store.read_raw(name).decode("utf-8-sig", errors="replace")
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
    """`ca.adjust` for each ingested session. Without the MCP servers its lookups of the next
    session's actions are alerted `corporate_action.unchecked` (to the log here) and the rest
    runs: the day's `bc` file, its dividends and demergers, the lineage refresh."""
    from ...daily.nightly import CA_ADJUST, INGEST_NIGHTLY
    from ...daily.nightly_ca import CaAdjust

    deps = _nightly_deps(ctx)
    ingested = _last_job(INGEST_NIGHTLY)
    days = [
        d
        for d in sessions_to_fill(ctx.sessions, _last_job(CA_ADJUST), ctx.through)
        if ingested is not None and d <= ingested
    ]
    return _each_session(ctx, CA_ADJUST, days, lambda day: lambda: CaAdjust(deps).run(day))


def _nightly_deps(ctx: Context):  # noqa: ANN202 - NightlyDeps for the data jobs only
    """The `NightlyDeps` `ca.adjust` reads (store, data sessions, clock, the NSE text files);
    the trading fields it never reads are inert."""
    from app.database import HatDataSessionLocal

    from ...clock import SystemClock
    from ...daily.nightly_deps import NightlyDeps, TextSources
    from ..bhavcopy import http_fetcher
    from ..nse_changes import NAME_CHANGES_URL, SYMBOL_CHANGES_URL
    from ..nse_files import EQUITY_LIST_URL, HOLIDAYS_URL, corporate_action_file, url_source

    fetch = http_fetcher()

    def unused(*_args: object) -> object:
        raise RuntimeError("not a data job's input")

    return NightlyDeps(
        store=_store(ctx),
        session_factory=HatDataSessionLocal,
        data_session_factory=HatDataSessionLocal,
        clock=SystemClock(),
        user_id=uuid.UUID(int=0),
        mcp=lambda: None,
        bhavcopy=lambda: fetch,
        sources=TextSources(
            equity_list=url_source(EQUITY_LIST_URL, fetch),
            holidays=url_source(HOLIDAYS_URL, fetch),
            price_bands=unused,
            symbol_changes=url_source(SYMBOL_CHANGES_URL, fetch),
            name_changes=url_source(NAME_CHANGES_URL, fetch),
            corporate_actions=lambda day: corporate_action_file(day, fetch),
        ),
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
    resolve: that needs the hand-kept `overrides.json` / `symbol_alias.json` (runbook)."""
    last = _covered_to()
    if last is not None and last >= ctx.through:
        return Done(last, "up to date")
    work = work_dir(ctx, "NIFTY500_DIR", "nifty500")
    csv = work / "nifty500-membership.csv"
    tool = "nifty500_membership.py"
    commands = [
        *(script(tool, "--work", work, step) for step in ("fetch", "text", "parse", "reference")),
        script(tool, "--work", work, "build", "--as-of", ctx.today),
        module("app.hat.admin", "load-index-membership", "--csv", csv, "--as-of", ctx.today)
        + ["--apply"],
    ]
    if ctx.dry_run:
        return Done(last, would(commands))
    try:
        chain(ctx, commands)
    except StepError as exc:
        raise StepError(exc.detail, _covered_to()) from None
    return Done(_covered_to(), f"loaded as of {ctx.today}")


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


def etf(ctx: Context) -> Done:
    """The overlay's raw files ask only for what is missing (the current windows), then `build`
    and `store` rewrite `HAT_DATA_ROOT/etf/`. `local` copies HAT's own files once."""
    work = work_dir(ctx, "OVERLAY_DATA_DIR", "overlay")
    steps = ("local", "kotak", "indices", "dividends", "build", "store")
    commands = [script("overlay_etf.py", "--work", work, s, kotak=s == "kotak") for s in steps]
    if ctx.dry_run:
        return Done(None, would(commands))
    chain(ctx, commands)
    return Done(ctx.through, "built and stored")


def results(ctx: Context) -> Done:
    steps = ("fetch", "fetch-secondary", "parse", "derive")
    commands = [module("app.hat.data.results", s) for s in steps]
    if ctx.dry_run:
        return Done(None, would(commands))
    chain(ctx, commands)
    return Done(ctx.through, "fetched, parsed, derived")


def fundamentals(ctx: Context) -> Done:
    commands = [module("app.hat.data.fundamentals", s) for s in ("extract", "derive")]
    if ctx.dry_run:
        return Done(None, would(commands))
    chain(ctx, commands)
    return Done(ctx.through, "extracted, derived")


BODIES: dict[str, Callable[[Context], Done]] = {
    "calendar": calendar,
    "bars": bars,
    "ingest": ingest,
    "quality": quality,
    "ca": ca,
    "features": features,
    "membership": membership,
    "bars5m": bars5m,
    "etf": etf,
    "results": results,
    "fundamentals": fundamentals,
}
