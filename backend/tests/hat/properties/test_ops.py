"""PBT for the job framework, the HAT-off gate and the close-gap log, the status route, the
backup manifest and the
deploy-window guard."""

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.clock import SYSTEM_CLOCK
from app.hat.daily.intraday import Intraday
from app.hat.daily.nightly import NightlyDeps
from app.hat.daily.runtime import ALERT_POOL, HatRuntime, daily_jobs, subscription_alert
from app.hat.data.subscriptions import LiveSubscriptions, SubscriptionInputs, SubscriptionRegistry
from app.hat.errors import HatConfigError
from app.hat.execution.session import (
    BACKOFF_CAP_SECONDS,
    WATCH_FROM,
    SessionWatch,
    backoff_delay,
)
from app.hat.logs import CONTEXT_KEYS, JsonFormatter
from app.hat.models import (
    HatEvent,
)
from app.hat.notify import RecordingNotifier
from app.hat.ops.jobs import (
    DEFAULT_ERROR_CODE,
    JobError,
)
from app.hat.ops.scheduler import Days, HatScheduler, ScheduledJob, due, runs_on
from app.hat.settings import load_settings
from app.hat.telegram.reports import Reports
from app.hat.time import IST, trade_date_of
from app.paths import PROJECT_ROOT
from app.utils.env_loader import TRUE_VALUES

ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


# ---------------------------------------------------------------------------------------
# nothing starts unless HAT_ENABLED is a true value
# ---------------------------------------------------------------------------------------

not_true = st.one_of(
    st.none(),
    st.text(max_size=12).filter(lambda raw: raw.strip().lower() not in TRUE_VALUES),
    st.sampled_from(["", " ", "false", "FALSE", "0", "no", "off", "2", "truee", "t", "y"]),
)
noise = st.dictionaries(
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=12).filter(
        lambda key: key not in ("HAT_ENABLED", "HAT_USER_ID")
    ),
    st.text(max_size=8),
    max_size=5,
)


def _runtime(settings_: object, dialect: str, touched: list[str]) -> HatRuntime:
    """A runtime whose database and parts only record that they were reached."""
    return HatRuntime(
        settings_,  # type: ignore[arg-type]
        lambda: touched.append("db"),  # type: ignore[arg-type,return-value]
        dialect_name=dialect,
        parts=lambda: touched.append("parts"),  # type: ignore[arg-type,return-value]
    )


@given(raw=not_true, other=noise, dialect=st.sampled_from(["sqlite", "postgresql", "mysql", ""]))
def test_disabled_starts_nothing(raw: str | None, other: dict[str, str], dialect: str) -> None:
    """With HAT_ENABLED unset or not a true value, `HatRuntime.start` starts no scheduler, task,
    feed, Telegram poller or log handler and reaches neither the database nor a broker, on any
    database dialect."""
    env = dict(other)
    if raw is not None:
        env["HAT_ENABLED"] = raw
    settings_ = load_settings(env)
    assert settings_.enabled is False

    hat_logger = logging.getLogger("app.hat")
    handlers_before = list(hat_logger.handlers)
    touched: list[str] = []
    runtime = _runtime(settings_, dialect, touched)

    async def start() -> tuple[bool, int]:
        started = await runtime.start()
        return started, len(asyncio.all_tasks())

    started, task_count = asyncio.run(start())
    assert started is False
    assert task_count == 1  # only this coroutine itself
    assert runtime.scheduler is None
    assert touched == []
    assert hat_logger.handlers == handlers_before


@given(
    raw=st.sampled_from(sorted(TRUE_VALUES)).flatmap(
        lambda v: st.sampled_from([v, v.upper(), f" {v} ", v.capitalize()])
    ),
    dialect=st.text(max_size=12).filter(lambda name: name != "postgresql"),
)
def test_refuses_non_postgres(raw: str, dialect: str) -> None:
    """Enabled on any other dialect, `HatRuntime.start` fails closed with
    `HatConfigError` before a task, a log handler, its parts or the database exist."""
    settings_ = load_settings({"HAT_ENABLED": raw})
    assert settings_.enabled is True
    hat_logger = logging.getLogger("app.hat")
    handlers_before = list(hat_logger.handlers)
    touched: list[str] = []
    runtime = _runtime(settings_, dialect, touched)

    async def start() -> int:
        with pytest.raises(HatConfigError):
            await runtime.start()
        return len(asyncio.all_tasks())

    assert asyncio.run(start()) == 1
    assert touched == []
    assert hat_logger.handlers == handlers_before


# ---------------------------------------------------------------------------------------
# trade-date jobs start only on trading days
# ---------------------------------------------------------------------------------------

CAL_START = date(2026, 1, 1)
job_specs = st.builds(
    lambda at, span, days, weekday: (at, span, days, weekday),
    st.times(),
    st.one_of(st.none(), st.integers(min_value=1, max_value=600)),
    st.sampled_from(list(Days)),
    st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
)


def _job(index: int, spec: tuple[time, int | None, Days, int | None]) -> ScheduledJob:
    at, span, days, weekday = spec
    until = None
    if span is not None:
        end = datetime.combine(CAL_START, at) + timedelta(minutes=span)
        until = end.time() if end.date() == CAL_START else None
    return ScheduledJob(f"job{index}", at, lambda _d: None, until=until, days=days, weekday=weekday)


@settings(max_examples=150)
@given(
    specs=st.lists(job_specs, min_size=1, max_size=6),
    holidays=st.sets(st.integers(min_value=0, max_value=27)),
    offset=st.integers(min_value=0, max_value=27 * 24 * 60),
)
def test_calendar_skip(
    specs: list[tuple[time, int | None, Days, int | None]], holidays: set[int], offset: int
) -> None:
    """For every calendar and every instant, a trade-date job is never due on a non-trading day
    (nor on a day the calendar does not know), an every-day job is not held back by the
    calendar, and the scheduler starts exactly the due jobs."""
    closed = {CAL_START + timedelta(days=d) for d in holidays}

    def trading(day: date) -> bool:
        return CAL_START <= day < CAL_START + timedelta(days=28) and day not in closed

    jobs = [_job(i, spec) for i, spec in enumerate(specs)]
    now = datetime.combine(CAL_START, time(0), tzinfo=IST) + timedelta(minutes=offset)
    today = now.astimezone(IST).date()
    scheduler = HatScheduler(jobs, trading=trading)
    started = {job.name for job, _ in scheduler.due_jobs(now, {})}
    for job in jobs:
        is_due = due(job, now, trading=trading, succeeded=(), attempted=None)
        assert (job.name in started) is is_due
        if job.days is Days.TRADING and not trading(today):
            assert is_due is False
        assert runs_on(job, today, trading) is (
            (job.weekday is None or today.weekday() == job.weekday)
            and (job.days is Days.EVERY or trading(today))
        )


CLOSE_AT, CLOSE_UNTIL = time(15, 20), time(15, 25)


# ---------------------------------------------------------------------------------------
# structured logs
# ---------------------------------------------------------------------------------------

levels = st.sampled_from([logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR])
context = st.fixed_dictionaries(
    {},
    optional={
        key: st.one_of(st.text(max_size=10), st.integers(), st.uuids()) for key in CONTEXT_KEYS
    },
)


@given(msg=st.text(max_size=60), level=levels, extra=context)
def test_log_records_structured(msg: str, level: int, extra: dict[str, object]) -> None:
    """Every record under `app.hat` renders as one JSON object line with ts, level, msg and
    each context key that was given, whatever characters the message holds."""
    record = logging.getLogger("app.hat.jobs").makeRecord(
        "app.hat.jobs", level, __file__, 1, "%s", (msg,), None, extra=extra
    )
    line = JsonFormatter().format(record)
    assert "\n" not in line
    entry = json.loads(line)
    assert isinstance(entry, dict)
    assert datetime.fromisoformat(entry["ts"]).utcoffset() == timedelta(0)
    assert entry["level"] == logging.getLevelName(level)
    assert entry["msg"] == msg
    for key in CONTEXT_KEYS:
        assert (key in entry) == (key in extra)
        if key in extra:
            assert entry[key] == str(extra[key])


# ---------------------------------------------------------------------------------------
# run_job, driven as a state machine on the real code
# ---------------------------------------------------------------------------------------

SECRET = "password=hunter2 host=10.0.0.7 ORA-00942"
codes = st.from_regex(r"[a-z][a-z0-9_.]{0,20}", fullmatch=True)


@given(code=st.text(max_size=80))
def test_job_error_codes_are_stable_codes(code: str) -> None:
    """A JobError carries either its own well-formed code or the generic one, never free text."""
    stored = JobError(code).code
    assert ERROR_CODE.fullmatch(stored)
    assert stored == code or stored == DEFAULT_ERROR_CODE


#: backoff and the one alert per outage


@given(st.lists(st.tuples(st.integers(min_value=0, max_value=400), st.booleans()), max_size=40))
def test_session_backoff_alert(steps: list[tuple[int, bool]]) -> None:
    """For every sequence of status checks, one `broker.session_lost` alert is due
    per outage, only once the outage has lasted 2 minutes, and retry delays never decrease
    within an outage (1 s, doubling, capped at 30 s)."""
    watch = SessionWatch()
    start = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    now = start
    down_since: datetime | None = None
    alerts = 0
    delays: list[int] = []
    for gap, live in steps:
        now += timedelta(seconds=gap)
        step = watch.observe(now, live)
        if live:
            assert step.retry_in is None
            assert not step.alert
            down_since, alerts, delays = None, 0, []
            continue
        down_since = down_since or now
        assert step.retry_in is not None
        assert 1 <= step.retry_in <= BACKOFF_CAP_SECONDS
        delays.append(step.retry_in)
        assert delays == sorted(delays)
        if step.alert:
            alerts += 1
            assert now - down_since >= timedelta(minutes=2)
        assert alerts <= 1
        if now - down_since >= timedelta(minutes=2):
            assert alerts == 1  # due as soon as an outage of 2 minutes has been observed


@given(
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=180), st.booleans()), min_size=1, max_size=40
    ),
    st.integers(min_value=0, max_value=20 * 60),
)
def test_no_outage_before_the_login_window(steps: list[tuple[int, bool]], start: int) -> None:
    """(design review item 5): an outage is counted from 08:30 IST, when the login
    prompt goes out. Checks before it (the night after the midnight token expiry, live or not)
    never alert and reset the watch, so a session down since midnight alerts once, 2 minutes
    after the first check from 08:30, and only if it is still down then. Harm: a
    `broker.session_lost` alert every night at 00:02, which trains the owner to ignore the
    alert that matters."""
    watch = SessionWatch()
    now = datetime.combine(date(2026, 10, 21), time(0), tzinfo=IST) + timedelta(minutes=start)
    down_since: datetime | None = None
    alerted = False
    for gap, live in steps:
        now += timedelta(minutes=gap)
        step = watch.observe(now, live)
        watched = now.astimezone(IST).time() >= WATCH_FROM
        if live or not watched:
            down_since, alerted = None, False
            assert not step.alert
            continue
        down_since = down_since or now
        due = not alerted and now - down_since >= timedelta(minutes=2)
        assert step.alert == due
        alerted = alerted or due
        assert datetime.combine(now.date(), WATCH_FROM, tzinfo=IST) <= down_since


@given(st.integers(min_value=-5, max_value=200))
def test_backoff_delay_is_monotone_and_capped(attempt: int) -> None:
    assert 1 <= backoff_delay(attempt) <= 30
    assert backoff_delay(attempt) <= backoff_delay(attempt + 1)


# ---------------------------------------------------------------------------------------
# the 15:25 live price and the official close of each MA exit evaluation
# ---------------------------------------------------------------------------------------

prices = st.decimals(min_value=50, max_value=150, places=2)


def _events(session: Session, kind: str) -> list[HatEvent]:
    return list(session.scalars(select(HatEvent).where(HatEvent.event_type == kind)))


# ---------------------------------------------------------------------------------------
# the backup manifest and the deploy-window guard (scripts/)
# ---------------------------------------------------------------------------------------


def _script(name: str) -> ModuleType:
    """A repo-root script as a module, without putting scripts/ on sys.path."""
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hat_backup = _script("hat_backup")
hat_deploy_guard = _script("hat_deploy_guard")

BACKUP_DIRS = ("", "raw/", "bars/2026/")
BACKUP_SUFFIXES = (".parquet", ".zip", ".dump", ".tar")
backup_names = st.builds(
    lambda folder, stem, suffix: f"{folder}{stem}{suffix}",
    st.sampled_from(BACKUP_DIRS),
    st.text(alphabet="abcxyz0189_-", min_size=1, max_size=6),
    st.sampled_from(BACKUP_SUFFIXES),
)
file_sets = st.dictionaries(backup_names, st.binary(min_size=1, max_size=64), min_size=1)
DAMAGE = {"flip": "checksum differs", "delete": "is missing", "grow": "has size", "cut": "has size"}


def _damage(path: Path, how: str, at: int) -> None:
    data = path.read_bytes()
    if how == "delete":
        path.unlink()
    elif how == "flip":
        index = at % len(data)
        path.write_bytes(data[:index] + bytes([data[index] ^ 0xFF]) + data[index + 1 :])
    elif how == "grow":
        path.write_bytes(data + b"\0")
    else:
        path.write_bytes(data[:-1])


@given(
    files=file_sets,
    target=st.sampled_from((0, 1)),
    how=st.sampled_from(sorted(DAMAGE)),
    pick=st.integers(min_value=0, max_value=10_000),
)
def test_backup_manifest(files: dict[str, bytes], target: int, how: str, pick: int) -> None:
    """For every file set, the manifest lists each file with its size and sha256,
    survives a JSON round trip, matches the tar of the set member for member, and verifies clean
    at both targets; then one flipped byte, missing file or size change at either target is
    reported for that file at that target, and only there. Harm: a damaged or partial backup
    passes as good and the event log or raw files cannot be restored."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        source = root / "source"
        for name, data in files.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_bytes(data)
        manifest = hat_backup.build_manifest(source, files)
        entries = manifest[hat_backup.FILES]
        assert sorted(entries) == sorted(files)
        for name, data in files.items():
            assert entries[name][hat_backup.SIZE] == len(data)
        assert json.loads(json.dumps(manifest)) == manifest

        tar_path = root / "set.tar"
        hat_backup.write_data_tar(source, sorted(files), tar_path)
        assert hat_backup.archive_manifest(tar_path) == entries

        targets = [root / "local", root / "remote"]
        for copy in targets:
            shutil.copytree(source, copy)
            assert hat_backup.verify_manifest(copy, manifest) == []

        name = sorted(files)[pick % len(files)]
        _damage(targets[target] / name, how, pick)
        problems = hat_backup.verify_manifest(targets[target], manifest)
        assert len(problems) == 1
        assert f": {name} {DAMAGE[how]}" in problems[0]
        assert hat_backup.verify_manifest(targets[1 - target], manifest) == []


WINDOW_OPEN_MINUTE = 8 * 60 + 30
WINDOW_CLOSE_MINUTE = 15 * 60 + 35
guard_zones = st.sampled_from((UTC, IST, ZoneInfo("America/New_York"), ZoneInfo("Asia/Tokyo")))
guard_instants = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31), timezones=guard_zones
)


def _in_window(now: datetime) -> bool:
    local = now.astimezone(IST)
    minute = local.hour * 60 + local.minute
    return WINDOW_OPEN_MINUTE <= minute < WINDOW_CLOSE_MINUTE


@given(now=guard_instants, listed=st.sampled_from((None, True, False)), force=st.booleans())
def test_deploy_window_guard(now: datetime, listed: bool | None, force: bool) -> None:
    """For every instant (in any time zone) and trading flag, a deploy is refused
    exactly when the IST time is in 08:30-15:35 on a trading day and it is not forced; a day
    the calendar does not list is a trading day iff it is a weekday (fail closed); the script
    exits non-zero exactly when refused, and a forced run inside the window appends one log
    line with its reason. Harm: a restart mid-session misses triggers, stop moves or the GTT
    swap."""
    day = now.astimezone(IST).date()
    calendar = {} if listed is None else {day: listed}
    trading = hat_deploy_guard.is_trading_day(day, calendar)
    assert trading == (day.weekday() < 5 if listed is None else listed)
    inside = _in_window(now)
    allowed = hat_deploy_guard.deploy_allowed(now, trading, force)
    assert allowed == (force or not (trading and inside))

    with (
        tempfile.TemporaryDirectory() as scratch,
        mock.patch.dict(os.environ, {"HAT_DATA_ROOT": scratch}),
    ):
        log = Path(scratch) / "ops" / "deploy_guard.jsonl"
        cal = Path(scratch) / "calendar.csv"
        cal.write_text("".join(f"{d},{'t' if t else 'f'}\n" for d, t in calendar.items()))
        argv = ["--calendar", str(cal)]
        argv += ["--force", "--reason", "drill"] if force else []
        code = hat_deploy_guard.main(argv, now)
        assert (code == 0) == (force or not (trading and inside))
        lines = log.read_text().splitlines() if log.exists() else []
        assert len(lines) == int(force and trading and inside)
        for line in lines:
            assert json.loads(line)["reason"] == "drill"


# ---------------------------------------------------------------------------------------
# GET /api/hat/status reports what the database holds
# ---------------------------------------------------------------------------------------

REGIMES = ("bull_quiet", "bull_volatile", "correction", "bear")
ACCOUNT = "account"
TRIPPED = "tripped"


def _account_jobs(exdate_check: bool) -> dict[str, ScheduledJob]:
    """The jobs `daily_jobs` registers for one broker account (stand-ins never called)."""
    account = uuid.UUID(int=1)
    nightly = NightlyDeps(**dict.fromkeys(NightlyDeps.__dataclass_fields__))
    nightly.clock = SYSTEM_CLOCK
    intraday = Intraday(
        **dict.fromkeys(
            (
                "session_factory",
                "clock",
                "book",
                "prices",
                "live",
                "service",
                "subscriptions",
                "shadow",
                "notifier",
                "gap_client",
                "calendar",
            )
        )
    )
    desks = SimpleNamespace(managers=lambda: [(account, None)])
    jobs = daily_jobs(
        nightly,
        intraday,
        desks,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        Reports(None, None),  # type: ignore[arg-type]
        exdate_check=exdate_check,
    )
    return {job.name.replace(str(account), "<account>"): job for job in jobs}


@settings(max_examples=30)
@given(
    raw=st.one_of(
        not_true,
        st.sampled_from(sorted(TRUE_VALUES)).flatmap(
            lambda v: st.sampled_from([v, v.upper(), f" {v} "])
        ),
    )
)
def test_ex_date_job_behind_flag(raw: str | None) -> None:
    """: `gtt.ex_date:<account>` is registered, at 08:40 on trading days and
    retried every minute, exactly when `HAT_EXDATE_GTT_CHECK` is a true value; unset (the
    default) or anything else registers no such job and leaves every other job as it was.
    Harm: the check runs on a GTT-book behaviour nobody has confirmed live, or the switch
    turns it on without the job ever being scheduled."""
    env = {} if raw is None else {"HAT_EXDATE_GTT_CHECK": raw}
    flag = load_settings(env).exdate_gtt_check
    assert flag == (raw is not None and raw.strip().lower() in TRUE_VALUES)
    jobs = _account_jobs(flag)
    plain = _account_jobs(False)
    job = jobs.get("gtt.ex_date:<account>")
    assert (job is not None) == flag
    assert {n: (j.at, j.after) for n, j in jobs.items() if n != "gtt.ex_date:<account>"} == {
        n: (j.at, j.after) for n, j in plain.items()
    }
    if job is not None:
        assert (job.at, job.days, job.retry_after) == (
            time(8, 40),
            Days.TRADING,
            timedelta(minutes=1),
        )


class ThreadNotifier(RecordingNotifier):
    """Records the thread of every alert; with `fail`, each send raises after it is recorded."""

    def __init__(self, fail: bool) -> None:
        super().__init__()
        self.fail = fail
        self.threads: list[threading.Thread] = []

    def alert(self, code: str, trade_date: date, detail: dict[str, str | int]) -> None:
        self.threads.append(threading.current_thread())
        super().alert(code, trade_date, detail)
        if self.fail:
            raise ConnectionError("transport down")


@given(
    sizes=st.lists(st.integers(0, 12), min_size=1, max_size=6),
    limit=st.integers(2, 10),
    now=st.datetimes(datetime(2026, 1, 1), datetime(2027, 1, 1), timezones=st.just(UTC)),
    fail=st.booleans(),
)
def test_subscription_limit_alerts(sizes: list[int], limit: int, now: datetime, fail: bool) -> None:
    """PBT ops.subscription_limit_alerts: the runtime's
    `subscription_alert` turns each sync whose wanted set (the symbols and the heartbeat)
    reached the provider's limit (a further symbol would get no ticks) into one health alert
    `subscription_limit` of the trade date of the runtime's clock, and a sync below it raises
    none. Every alert is sent on `ALERT_POOL`, never on the syncing thread (a job holding the
    subscriptions' lock), and a failed send neither reaches the sync nor stops later alerts.
    Harm: the owner never hears that symbols past the limit get no ticks, so their bars,
    triggers and live exits stay silent; or a slow transport stalls every subscription sync."""
    notifier = ThreadNotifier(fail)
    subs = LiveSubscriptions(
        SubscriptionRegistry(str.upper),
        lambda _s: None,
        lambda _s: None,
        subscription_alert(notifier, lambda: now),
        limit=limit,
    )
    for size in sizes:
        watchlist = frozenset(f"NSE:S{n}-EQ" for n in range(size))
        subs.sync(SubscriptionInputs(watchlist, frozenset(), frozenset()))
    ALERT_POOL.submit(lambda: None).result()  # the one worker has sent every alert before this
    wanted = [size + 1 for size in sizes]  # the heartbeat is always held
    assert notifier.alerts == [
        ("subscription_limit", trade_date_of(now), {}) for n in wanted if n >= limit
    ]
    assert threading.current_thread() not in notifier.threads
