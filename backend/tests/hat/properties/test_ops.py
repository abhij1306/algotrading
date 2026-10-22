"""PBT for the job framework, the HAT-off gate and the close-gap log (HAT-OPS-010, -011, -013,
-034, -070), the status route (HAT-OPS-072), the backup manifest (HAT-OPS-060) and the
deploy-window guard (HAT-OPS-042)."""

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
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from hat_dashboard_support import API, NOW, TODAY, dashboard_client, instant
from hat_engine_support import TRADE_DAY, exit_stack, open_trade, seed_engine_world
from hat_risk_support import seed_bot
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.daily import live as live_module
from app.hat.daily.live import GateBoard
from app.hat.daily.market import DailyData
from app.hat.daily.runtime import HatRuntime
from app.hat.data.store import BarRow, DataStore
from app.hat.data.ticks import tick_for_date
from app.hat.engine import exits, live_trades, trades
from app.hat.errors import HatConfigError
from app.hat.execution.session import (
    BACKOFF_CAP_SECONDS,
    WATCH_FROM,
    SessionWatch,
    backoff_delay,
)
from app.hat.logs import CONTEXT_KEYS, JsonFormatter
from app.hat.models import (
    HatAllocationDaily,
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatEvent,
    HatJobRun,
    HatKillSwitch,
    HatMarket,
    HatModeChange,
    HatRegimeState,
    HatRiskPolicyVersion,
    HatStrategy,
    HatTradingCalendar,
)
from app.hat.models.ops import JOB_STATUSES
from app.hat.ops.jobs import (
    DEFAULT_ERROR_CODE,
    JobError,
    JobResult,
    RunStatus,
    _claim,
    recover_interrupted,
    run_job,
)
from app.hat.ops.scheduler import CARRY_UNTIL, Days, HatScheduler, ScheduledJob, due, runs_on
from app.hat.risk import kill
from app.hat.risk.sessions import SessionCalendar
from app.hat.settings import load_settings
from app.hat.time import IST, day_bounds
from app.paths import PROJECT_ROOT
from app.utils.env_loader import TRUE_VALUES

JOBS = ("ingest.nightly", "scan")
DATES = (date(2026, 10, 19), date(2026, 10, 20))
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


# ---------------------------------------------------------------------------------------
# HAT-OPS-013 / HAT-OPS-001: nothing starts unless HAT_ENABLED is a true value
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
    """HAT-OPS-002: enabled on any other dialect, `HatRuntime.start` fails closed with
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
# HAT-OPS-012: trade-date jobs start only on trading days
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


RESTART_DAY = date(2026, 10, 21)
CLOSE_AT, CLOSE_UNTIL = time(15, 20), time(15, 25)


def _ist(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=IST)


@settings(max_examples=12, deadline=None)
@given(pending=st.integers(min_value=1, max_value=40))
def test_window_not_starved(pending: int) -> None:
    """HAT-EXEC-074 (audit money-safety-engine-ops F12): after a restart at 15:19 that starts
    `pending` overdue morning jobs whose bodies block, the 15:20 tick still runs `exits.close`
    inside its window, while every morning job is still running."""
    release, closed = threading.Event(), threading.Event()
    jobs = [
        ScheduledJob(f"morning{i}", time(8, 45), lambda _d: release.wait(10))
        for i in range(pending)
    ]
    jobs.append(ScheduledJob("exits.close", CLOSE_AT, lambda _d: closed.set(), until=CLOSE_UNTIL))
    clock = [_ist(RESTART_DAY, time(15, 19))]
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(
            f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False, "timeout": 30}
        )
        HatJobRun.__table__.create(engine)
        scheduler = HatScheduler(jobs, sessionmaker(engine), lambda: clock[0])

        async def restart() -> bool:
            await scheduler.tick()
            clock[0] = _ist(RESTART_DAY, CLOSE_AT)
            await scheduler.tick()
            for _ in range(100):  # up to 5 s, polled on the loop (no pool thread needed)
                if closed.is_set():
                    break
                await asyncio.sleep(0.05)
            ran = closed.is_set()
            release.set()
            await scheduler.drain()
            await scheduler.stop()
            return ran

        ran = asyncio.run(restart())
        engine.dispose()
    assert ran


def _job_engine(tmp: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False, "timeout": 30}
    )
    HatJobRun.__table__.create(engine)
    return engine


@settings(max_examples=25, deadline=None)
@given(
    run_minutes=st.integers(min_value=0, max_value=300),
    wait=st.integers(min_value=0, max_value=20),
)
def test_retry_after_failed_run(run_minutes: int, wait: int) -> None:
    """HAT-OPS-021 (`retry_after`): a failed run is tried again `retry_after` after it ended,
    however long it ran. Harm: the ingest that waited four hours for its data and failed at 22:01
    is started again at once, a second download and failure cycle with no back-off."""
    clock = [_ist(RESTART_DAY, time(18, 0))]

    def body(_day: date) -> None:
        clock[0] += timedelta(minutes=run_minutes)  # the run takes this long, then fails
        raise JobError("source_unavailable")

    job = ScheduledJob("ingest.nightly", time(18, 0), body, retry_after=timedelta(minutes=10))
    with tempfile.TemporaryDirectory() as tmp:
        engine = _job_engine(tmp)
        scheduler = HatScheduler([job], sessionmaker(engine), lambda: clock[0])

        async def run() -> tuple[int, int]:
            first = len(await scheduler.tick())
            await scheduler.drain()
            clock[0] += timedelta(minutes=wait)
            again = len(await scheduler.tick())
            await scheduler.drain()
            await scheduler.stop()
            return first, again

        first, again = asyncio.run(run())
        engine.dispose()
    assert first == 1
    assert (again == 1) is (wait >= 10)


NIGHT_DAY = date(2026, 10, 20)  # a Tuesday; the 21st is a trading day too


@settings(max_examples=40, deadline=None)
@given(
    head_done=st.integers(min_value=0, max_value=359),  # minutes after 18:00 on NIGHT_DAY
    later=st.integers(min_value=1, max_value=16 * 60),
    kind=st.sampled_from(("chained", "carry", "plain")),
)
def test_chain_carries_past_midnight(head_done: int, later: int, kind: str) -> None:
    """HAT-OPS-021 (audit L6): a chained job whose `after` job succeeded late on its date is
    still started under that trade date after IST midnight, until `CARRY_UNTIL`, and never under
    the next date before its own chain ran. A job with no `after` marked `carry`
    (`snapshot.bots`, PR 33) carries over the same way; an unmarked one does not. Harm: an
    ingest that succeeds at 23:58, or a restart at 00:10, leaves no quality gate, allocation,
    scan or drawdown-kill evaluation for its day."""
    clock = [_ist(NIGHT_DAY, time(18, 0)) + timedelta(minutes=head_done)]
    started: list[tuple[str, date]] = []
    jobs = [
        ScheduledJob("head", time(18, 0), lambda _d: None),
        ScheduledJob(
            "next",
            time(18, 0),
            lambda d: started.append(("next", d)),
            after=("head",) if kind == "chained" else (),
            carry=kind == "carry",
        ),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        engine = _job_engine(tmp)
        with Session(engine) as session:
            session.add(
                HatJobRun(
                    job="head",
                    trade_date=NIGHT_DAY,
                    started_at=clock[0],
                    finished_at=clock[0],
                    status="succeeded",
                )
            )
            session.commit()
        scheduler = HatScheduler(jobs[1:], sessionmaker(engine), lambda: clock[0])
        tick_at = clock[0] + timedelta(minutes=later)
        clock[0] = tick_at

        async def run() -> None:
            await scheduler.tick()
            await scheduler.drain()
            await scheduler.stop()

        asyncio.run(run())
        engine.dispose()
    local = tick_at.astimezone(IST)
    carried = (
        kind != "plain"
        and local.date() == NIGHT_DAY + timedelta(days=1)
        and local.time() < CARRY_UNTIL
    )
    expected = [("next", NIGHT_DAY)] if local.date() == NIGHT_DAY or carried else []
    assert started == expected


@settings(max_examples=10, deadline=None)
@given(stuck=st.lists(st.booleans(), min_size=1, max_size=4))
def test_stop_names_abandoned_jobs(stuck: list[bool]) -> None:
    """Audit L9: `stop` waits up to its grace for the running bodies, and names (and logs) each
    one still running then. Harm: a close or swap cut off by a shutdown leaves no trace of which
    job was abandoned, so a position left on its day stop is found only by chance."""
    release = threading.Event()

    def held(_day: date) -> None:  # returns nothing: a body's result is a JobResult or None
        release.wait(10)

    jobs = [
        ScheduledJob(f"job{i}", time(9, 0), held if blocked else (lambda _d: None))
        for i, blocked in enumerate(stuck)
    ]
    clock = [_ist(RESTART_DAY, time(9, 0))]
    with tempfile.TemporaryDirectory() as tmp:
        engine = _job_engine(tmp)
        scheduler = HatScheduler(jobs, sessionmaker(engine), lambda: clock[0])

        async def run() -> list[str]:
            await scheduler.tick()
            await asyncio.sleep(0.2)
            abandoned = await scheduler.stop(grace=0.3)
            release.set()
            return abandoned

        abandoned = asyncio.run(run())
        _await_runs_finished(engine)
        engine.dispose()
    assert abandoned == sorted(f"job{i}" for i, blocked in enumerate(stuck) if blocked)


def _await_runs_finished(engine: Engine, timeout: float = 10.0) -> None:
    """Wait until every `hat_job_run` row has its finish: an abandoned body, once released, still
    records its run from the scheduler's own thread, and the temporary database must not be
    removed under that write."""
    pause = threading.Event()
    for _ in range(int(timeout / 0.02)):
        with Session(engine) as session:
            if session.scalar(select(HatJobRun.id).where(HatJobRun.finished_at.is_(None))) is None:
                return
        pause.wait(0.02)
    raise AssertionError("an abandoned job never recorded its finish")


class _StopFeed:
    """The feed's stop half: listeners and stop calls, counted."""

    def __init__(self) -> None:
        self.listeners: list[object] = []
        self.stopped = 0

    def add_tick_listener(self, listener: object) -> object:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def stop(self) -> None:
        self.stopped += 1


class _Failing:
    """A built part whose stop step raises."""

    def detach(self) -> None:
        raise RuntimeError

    def release_all(self) -> None:
        raise RuntimeError


@settings(max_examples=60)
@given(
    built=st.integers(min_value=0, max_value=3),
    attached=st.booleans(),
    failing=st.sets(st.sampled_from(["live", "subscriptions"])),
)
def test_stop_after_failed_start(built: int, attached: bool, failing: set[str]) -> None:
    """Audit L8: after a start that failed part way (parts read, then none, some or all of the
    live data and subscriptions built), `stop` runs each step on what exists, and the feed is
    stopped exactly once with no tick listener left, even when a step raises. Harm: a skipped
    `feed.stop` leaves the provider's login hooks and socket behind a failed start."""
    feed = _StopFeed()
    runtime = _runtime(load_settings({"HAT_ENABLED": "true"}), "postgresql", [])
    if built:
        runtime.parts = SimpleNamespace(feed=feed)  # type: ignore[attr-defined]
    if built >= 2:
        live = SimpleNamespace(detach=lambda: None)
        if attached:
            remove = feed.add_tick_listener(object())
            live = SimpleNamespace(detach=remove)
        runtime.live = _Failing() if "live" in failing else live  # type: ignore[attr-defined]
    if built >= 3:
        released = SimpleNamespace(release_all=lambda: None)
        runtime.subscriptions = (  # type: ignore[attr-defined]
            _Failing() if "subscriptions" in failing else released
        )
    asyncio.run(runtime.stop())
    assert feed.stopped == (1 if built else 0)
    left = 1 if built >= 2 and attached and "live" in failing else 0
    assert len(feed.listeners) == left
    assert runtime.started is False


TICK_DAYS = [date(2026, 8, 24) + timedelta(days=i) for i in range(28) if (i % 7) < 5]
tick_closes = st.dictionaries(  # (instrument, session) -> the closes of its ingest runs, in order
    st.tuples(st.integers(min_value=0, max_value=2), st.sampled_from(TICK_DAYS)),
    st.lists(
        st.decimals(min_value=Decimal("0.05"), max_value=Decimal(9000), places=2),
        min_size=1,
        max_size=3,
    ),
    max_size=24,
)


@settings(max_examples=40, deadline=None)
@given(closes=tick_closes, trade_at=st.integers(min_value=1, max_value=len(TICK_DAYS)))
def test_daily_tick_matches_reference(
    closes: dict[tuple[int, date], list[Decimal]], trade_at: int
) -> None:
    """Simplify #21: `DailyData.tick` reads only the two closes `reference_close` can pick (the
    last before the month start and the latest, each date's from its newest ingest run), and
    gives the tick `tick_for_date` gives over every close of the window. Harm: a wrong tick puts
    a stop or a limit off the exchange's grid, and the order is rejected."""
    ids = [uuid.UUID(int=n + 1) for n in range(3)]
    trade_date = TICK_DAYS[trade_at] if trade_at < len(TICK_DAYS) else date(2026, 9, 21)
    engine = memory_engine()
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        runs: dict[date, dict[int, list[BarRow]]] = {}
        for (n, day), prices in closes.items():
            for run, price in enumerate(prices):
                row = BarRow(
                    *(ids[n], f"INE{n:09d}", f"S{n}", "EQ", price, price, price, price, price),
                    *(1000, None, "bhavcopy", uuid.UUID(int=run + 1)),
                )
                runs.setdefault(day, {}).setdefault(run, []).append(row)
        for day, by_run in runs.items():
            for rows in by_run.values():
                store.write_bars(day, rows)
        with Session(engine) as session:
            data = DailyData(store, session, trade_date)
        engine.dispose()
    for n, instrument in enumerate(ids):
        mine = sorted((d, p[-1]) for (i, d), p in closes.items() if i == n and d < trade_date)
        want = tick_for_date([d for d, _ in mine], [c for _, c in mine], trade_date)
        assert data.tick(instrument) == want


RUN_STATUSES = (None, *JOB_STATUSES)


@settings(max_examples=150)
@given(
    trading=st.lists(st.booleans(), min_size=1, max_size=8),
    statuses=st.lists(st.sampled_from(RUN_STATUSES), min_size=8, max_size=8),
    allocated=st.sets(st.integers(min_value=0, max_value=7)),
    known=st.booleans(),
)
def test_entry_inputs_fail_closed(
    trading: list[bool], statuses: list[str | None], allocated: set[int], known: bool
) -> None:
    """HAT-OPS-021, HAT-PORT-053: an entry on day d reads the quality state and the allocation of
    the night before only (the previous trading day of the calendar): the quality state is
    failed or passed only when that night's `quality.gate` run failed or succeeded, else unknown
    (entries fail closed), and the allocation is that night's row or none, never an older one;
    without a calendar both are unknown."""
    days = [CAL_START + timedelta(days=i) for i in range(len(trading))]
    calendar = (
        SessionCalendar.from_rows((d, t, None, None) for d, t in zip(days, trading, strict=True))
        if known
        else None
    )
    engine = memory_engine()
    with Session(engine) as session:
        bot = seed_bot(session)
        for day, status in zip(days, statuses, strict=False):
            if status is not None:
                session.add(HatJobRun(job="quality.gate", trade_date=day, status=status))
        for i in allocated:
            session.add(_allocation_row(CAL_START + timedelta(days=i), bot))
        session.commit()
        for i, day in enumerate(days):
            before = [j for j in range(i) if trading[j]] if known else []
            night = before[-1] if before else None
            status = statuses[night] if night is not None else None
            expected = {"failed": True, "succeeded": False}.get(status or "")
            assert GateBoard.quality_failed(session, day, calendar) is expected
            row = live_module._allocation(session, bot, day, calendar)
            wanted = night if night is not None and night in allocated else None
            assert (row.trade_date if row else None) == (
                days[wanted] if wanted is not None else None
            )


def _allocation_row(day: date, bot: uuid.UUID) -> HatAllocationDaily:
    one = Decimal(1)
    return HatAllocationDaily(
        trade_date=day,
        bot_id=bot,
        base_weight=one,
        regime_mult=one,
        health_mult=one,
        final_weight=one,
        capital=Decimal(100000),
        risk_pct=Decimal("0.01"),
        currency="INR",
    )


# ---------------------------------------------------------------------------------------
# HAT-OPS-070: structured logs
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
# HAT-OPS-010 / HAT-OPS-011: run_job, driven as a state machine on the real code
# ---------------------------------------------------------------------------------------

SECRET = "password=hunter2 host=10.0.0.7 ORA-00942"
codes = st.from_regex(r"[a-z][a-z0-9_.]{0,20}", fullmatch=True)


class JobRunMachine(RuleBasedStateMachine):
    """Random calls of `run_job` (bodies that succeed, fail with a code, or raise raw text),
    simulated crashes (a claimed run never finished) and restarts."""

    def __init__(self) -> None:
        super().__init__()
        self.engine = memory_engine(HatJobRun)
        self.factory: sessionmaker[Session] = sessionmaker(bind=self.engine)
        self.expected: dict[tuple[str, date], str] = {}  # model of each run's status
        self.successful_bodies: dict[tuple[str, date], int] = {}

    def teardown(self) -> None:
        self.engine.dispose()

    @rule(
        job=st.sampled_from(JOBS),
        day=st.sampled_from(DATES),
        kind=st.sampled_from(["ok", "none", "code", "raw"]),
        calls=st.integers(0, 50),
        rows=st.integers(0, 10**6),
        code=codes,
    )
    def call_run_job(
        self, job: str, day: date, kind: str, calls: int, rows: int, code: str
    ) -> None:
        key = (job, day)
        before = self.expected.get(key)
        executed: list[bool] = []

        def body() -> JobResult | None:
            executed.append(True)
            if kind == "ok":
                return JobResult(calls=calls, rows=rows)
            if kind == "none":
                return None
            if kind == "code":
                raise JobError(code)
            raise RuntimeError(SECRET)

        outcome = run_job(job, day, body, session_factory=self.factory)

        if before == "succeeded":
            # A success is final: the stored result comes back and the body never runs.
            assert not executed
            assert outcome.status is RunStatus.SUCCEEDED
            assert not outcome.executed
        elif before == "running":
            assert not executed
            assert outcome.status is RunStatus.RUNNING
        else:
            # No run yet, or the last run failed or was interrupted: the body runs again.
            assert executed
            assert outcome.executed
            if kind in ("ok", "none"):
                assert outcome.status is RunStatus.SUCCEEDED
                self.expected[key] = "succeeded"
                self.successful_bodies[key] = self.successful_bodies.get(key, 0) + 1
                assert (outcome.calls, outcome.rows) == ((calls, rows) if kind == "ok" else (0, 0))
                assert outcome.error is None
            else:
                assert outcome.status is RunStatus.FAILED
                self.expected[key] = "failed"
                assert outcome.error is not None
                assert outcome.error == (code if kind == "code" else DEFAULT_ERROR_CODE)

    @rule(job=st.sampled_from(JOBS), day=st.sampled_from(DATES))
    def crash_mid_run(self, job: str, day: date) -> None:
        """A process dies after claiming a run: the row stays `running`."""
        key = (job, day)
        if self.expected.get(key) in ("succeeded", "running"):
            return
        run_id, stored = _claim(self.factory, job, day, datetime.now(UTC))
        assert run_id is not None
        assert stored is None
        self.expected[key] = "running"

    @rule()
    def restart(self) -> None:
        """A restart reaps every run still `running` as failed (interrupted)."""
        recovered = recover_interrupted(self.factory)
        assert recovered == sum(1 for status in self.expected.values() if status == "running")
        for key, status in self.expected.items():
            if status == "running":
                self.expected[key] = "failed"

    @invariant()
    def stored_rows_match_the_model(self) -> None:
        with self.factory() as session:
            rows = session.scalars(select(HatJobRun)).all()
        assert {(row.job, row.trade_date): row.status for row in rows} == self.expected
        for row in rows:
            started = row.started_at.replace(tzinfo=UTC)
            if row.status == "running":
                assert row.finished_at is None
            else:
                assert row.finished_at is not None
                assert row.finished_at.replace(tzinfo=UTC) >= started
            if row.status == "failed":
                assert row.error
                assert ERROR_CODE.fullmatch(row.error)
                assert SECRET not in row.error
            else:
                assert row.error is None

    @invariant()
    def a_body_succeeds_at_most_once_per_key(self) -> None:
        assert all(count <= 1 for count in self.successful_bodies.values())


def test_job_run_recording() -> None:
    # 60 sequences of 40 steps: each step writes to a real database, so this is the one
    # property run below the profile's 500 examples (recorded under "Scope changes" in
    # .claude/skills/verify-scheduler/SKILL.md).
    run_state_machine_as_test(
        JobRunMachine, settings=settings(stateful_step_count=40, max_examples=60, deadline=None)
    )


@given(code=st.text(max_size=80))
def test_job_error_codes_are_stable_codes(code: str) -> None:
    """A JobError carries either its own well-formed code or the generic one, never free text."""
    stored = JobError(code).code
    assert ERROR_CODE.fullmatch(stored)
    assert stored == code or stored == DEFAULT_ERROR_CODE


# HAT-OPS-031 (PR 13: the session side): backoff and the one alert per outage


@given(st.lists(st.tuples(st.integers(min_value=0, max_value=400), st.booleans()), max_size=40))
def test_session_backoff_alert(steps: list[tuple[int, bool]]) -> None:
    """HAT-OPS-031: for every sequence of status checks, one `broker.session_lost` alert is due
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
    """HAT-OPS-031 (design review item 5): an outage is counted from 08:30 IST, when the login
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
# HAT-OPS-034: the 15:25 live price and the official close of each MA exit evaluation
# ---------------------------------------------------------------------------------------

prices = st.decimals(min_value=50, max_value=150, places=2)


@settings(max_examples=25, deadline=None)
@given(
    marks=st.lists(st.tuples(prices, prices, st.one_of(st.none(), prices)), min_size=1, max_size=3),
    late=st.booleans(),
)
def test_close_gap_recorded(
    marks: list[tuple[Decimal, Decimal, Decimal | None]], late: bool
) -> None:
    """HAT-OPS-034: every `close_below_sma` evaluation in the close window records the live
    price it used, fired or not; once the nightly ingest has the official close, exactly one
    `exit.close_gap` per position records that close and its gap to the live price, against the
    same position. A close the ingest does not have yet is recorded by a later run, and a run
    repeated changes nothing. Harm: the backtest/live gap of MA exits cannot be measured."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, exits=exit_stack("trend"), instruments=len(marks))
        at = datetime.combine(TRADE_DAY, time(15, 22), tzinfo=IST)
        used: dict[uuid.UUID, tuple[uuid.UUID, Decimal]] = {}
        for (live, sma, _), instrument in zip(marks, world.instruments, strict=True):
            position_id = open_trade(session, world, instrument).outcome.position_id
            assert position_id is not None
            view = exits.ExitView(price=live, days_held=1, sma={10: sma})
            result = trades.evaluate_position(session, position_id, view, "close", at)
            assert live_trades.record_close_evaluation(
                session, position_id, view, result, TRADE_DAY, at
            )
            session.commit()
            used[position_id] = (instrument, live)
        evaluated = {
            event.aggregate_id: Decimal(event.payload["live_price"])
            for event in _events(session, live_trades.CLOSE_EVALUATED)
        }
        assert evaluated == {pid: live for pid, (_, live) in used.items()}

        closes = {world.instruments[n]: close for n, (_, _, close) in enumerate(marks) if close}
        night = datetime.combine(TRADE_DAY, time(21, 0), tzinfo=IST)
        first = live_trades.record_close_gaps(session, TRADE_DAY, closes, night)
        session.commit()
        assert first == len(closes)
        if late:  # the ingest has every close the next night
            every = {
                world.instruments[n]: close or live for n, (live, _, close) in enumerate(marks)
            }
            live_trades.record_close_gaps(session, TRADE_DAY, every, night + timedelta(hours=20))
            closes = every
        again = live_trades.record_close_gaps(session, TRADE_DAY, closes, night)
        session.commit()
        assert again == 0

        gaps = _events(session, live_trades.CLOSE_GAP)
        assert sorted(str(e.aggregate_id) for e in gaps) == sorted(
            str(pid) for pid, (instrument, _) in used.items() if instrument in closes
        )
        for event in gaps:
            instrument, live = used[event.aggregate_id]
            official = closes[instrument]
            assert Decimal(event.payload["live_price"]) == live
            assert Decimal(event.payload["official_close"]) == official
            assert Decimal(event.payload["gap"]) == official - live


def _events(session: Session, kind: str) -> list[HatEvent]:
    return list(session.scalars(select(HatEvent).where(HatEvent.event_type == kind)))


# ---------------------------------------------------------------------------------------
# HAT-OPS-060 / HAT-OPS-042: the backup manifest and the deploy-window guard (scripts/)
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
    """HAT-OPS-060: for every file set, the manifest lists each file with its size and sha256,
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
    """HAT-OPS-042: for every instant (in any time zone) and trading flag, a deploy is refused
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
# HAT-OPS-072: GET /api/hat/status reports what the database holds
# ---------------------------------------------------------------------------------------

STATUS_JOBS = ("ingest.nightly", "scan", "exits.close")
STATUS_TABLES = (
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatEvent,
    HatJobRun,
    HatKillSwitch,
    HatMarket,
    HatModeChange,
    HatRegimeState,
    HatRiskPolicyVersion,
    HatStrategy,
    HatTradingCalendar,
)
REGIMES = ("bull_quiet", "bull_volatile", "correction", "bear")
KILL_BASE = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
ACCOUNT = "account"
TRIPPED = "tripped"

# (bot index or None for the account, reason, state, minute): minutes are unique, so the latest
# row of every key is unambiguous.
kill_rows = st.lists(
    st.tuples(
        st.one_of(st.none(), st.integers(0, 2)),
        st.sampled_from(kill.REASONS),
        st.sampled_from([TRIPPED, "armed"]),
        st.integers(0, 2000),
    ),
    max_size=8,
    unique_by=lambda row: row[3],
)
status_worlds = st.fixed_dictionaries(
    {
        "bots": st.lists(
            st.tuples(
                st.sampled_from(["shadow", "confirm", "auto"]),
                st.sampled_from(["active", "paused", "manage_only"]),
            ),
            max_size=3,
        ),
        "kills": kill_rows,
        "jobs": st.lists(
            st.tuples(
                st.sampled_from(STATUS_JOBS),
                st.sampled_from([TODAY - timedelta(days=1), TODAY]),
                st.sampled_from(JOB_STATUSES),
            ),
            max_size=6,
            unique_by=lambda job: job[:2],
        ),
        "regimes": st.lists(
            st.tuples(st.integers(0, 30), st.sampled_from(REGIMES), st.booleans()),
            max_size=4,
            unique_by=lambda regime: regime[0],
        ),
        "accounts": st.lists(
            st.tuples(st.booleans(), st.sampled_from([None, "live", "expired"])), max_size=3
        ),
        "stale": st.lists(st.integers(-1500, 1500), max_size=3),
    }
)


def _status_bot(n: int) -> uuid.UUID:
    return uuid.UUID(int=0xB0 + n)


def _seed_bots(session: Session, bots: list[tuple[str, str]]) -> None:
    for n, (mode, status) in enumerate(bots):
        strategy = uuid.uuid4()
        session.add(
            HatStrategy(
                id=strategy,
                user_id=uuid.uuid4(),
                market_id=uuid.uuid4(),
                name=f"bot{n}",
                persona="trader",
            )
        )
        session.add(
            HatBot(
                id=_status_bot(n),
                user_id=uuid.uuid4(),
                strategy_id=strategy,
                broker_account_id=uuid.uuid4(),
                execution_mode=mode,
                status=status,
                lifecycle_state="live",
                risk_cluster="c1",
            )
        )


def _kill_row(who: int | None, reason: str, state: str, minute: int) -> HatKillSwitch:
    """A trip at `minute`, or a re-arm at `minute` of a trip the day before."""
    at = KILL_BASE + timedelta(minutes=minute)
    tripped = state == TRIPPED
    return HatKillSwitch(
        scope=ACCOUNT if who is None else "bot",
        bot_id=None if who is None else _status_bot(who),
        state=state,
        tripped_at=at if tripped else KILL_BASE - timedelta(days=1),
        rearmed_at=None if tripped else at,
        reason=reason,
    )


def _job_row(job: str, day: date, status: str) -> HatJobRun:
    started = datetime.combine(day, time(3, 0), tzinfo=UTC)
    return HatJobRun(
        job=job,
        trade_date=day,
        status=status,
        started_at=started,
        calls=1,
        rows=2,
        finished_at=None if status == "running" else started + timedelta(minutes=5),
        error="job.failed" if status == "failed" else None,
    )


def _seed_accounts(session: Session, accounts: list[tuple[bool, str | None]]) -> None:
    for n, (active, login) in enumerate(accounts):
        account = uuid.UUID(int=0xA0 + n)
        session.add(
            HatBrokerAccount(
                id=account,
                user_id=uuid.uuid4(),
                broker="fyers",
                account_ref=f"acct{n}",
                capabilities={},
                status="active" if active else "closed",
            )
        )
        if login is not None:
            session.add(HatBrokerSession(broker_account_id=account, trade_date=TODAY, status=login))


def _seed_status_world(session: Session, world: dict) -> None:
    _seed_bots(session, world["bots"])
    session.add_all(_kill_row(*row) for row in world["kills"])
    session.add_all(_job_row(*job) for job in world["jobs"])
    model = uuid.uuid4()
    session.add_all(
        HatRegimeState(
            trade_date=TODAY - timedelta(days=back),
            market_id=model,
            model_version_id=model,
            state=state,
            inputs={},
            confirmed=confirmed,
        )
        for back, state, confirmed in world["regimes"]
    )
    _seed_accounts(session, world["accounts"])
    session.add_all(
        HatEvent(
            id=n,
            occurred_at=NOW + timedelta(minutes=minute),
            user_id=uuid.uuid4(),
            aggregate_type="session",
            aggregate_id=uuid.uuid4(),
            event_type="feed.stale",
            actor="engine",
            payload={},
        )
        for n, minute in enumerate(world["stale"], start=1)
    )
    session.commit()


def _expected_kills(world: dict) -> dict[tuple[str, uuid.UUID | None, str], str]:
    """The state of each (scope, bot, channel) key: tripped when the latest row of any of its
    streams (the risk reasons together, each health reason alone) is a trip."""
    latest: dict[tuple[str, uuid.UUID | None, str, str], tuple[int, str]] = {}
    for who, reason, state, minute in world["kills"]:
        health = reason in kill.HEALTH_REASONS
        channel, stream = ("health", reason) if health else ("risk", "risk")
        scope = (ACCOUNT, None) if who is None else ("bot", _status_bot(who))
        key = (*scope, channel, stream)
        if key not in latest or latest[key][0] < minute:
            latest[key] = (minute, state)
    states: dict[tuple[str, uuid.UUID | None, str], str] = {}
    for (scope_name, bot, channel, _), (_, state) in latest.items():
        if states.get((scope_name, bot, channel)) != TRIPPED:
            states[(scope_name, bot, channel)] = state
    return states


def _expected_bots(world: dict, kills: dict) -> dict[str, tuple[str, str, list[str]]]:
    def tripped(scope: str, bot: uuid.UUID | None, channel: str) -> bool:
        return kills.get((scope, bot, channel)) == TRIPPED

    expected = {}
    for n, (mode, status) in enumerate(world["bots"]):
        bot = _status_bot(n)
        flags = [
            ("account_kill", tripped(ACCOUNT, None, "risk")),
            ("bot_kill", tripped("bot", bot, "risk")),
            ("health_kill", tripped("bot", bot, "health") or tripped(ACCOUNT, None, "health")),
            ("paused", status != "active"),
        ]
        expected[str(bot)] = (mode, status, [name for name, on in flags if on])
    return expected


def _expected_jobs(world: dict) -> dict[str, str]:
    return {job: status for job, day, status in world["jobs"] if day == TODAY}


def _expected_regime(world: dict) -> dict | None:
    if not world["regimes"]:
        return None
    back, state, confirmed = min(world["regimes"])
    day = (TODAY - timedelta(days=back)).isoformat()
    return {"trade_date": day, "state": state, "confirmed": confirmed}


def _expected_stale(world: dict) -> datetime | None:
    start, end = day_bounds(TODAY)
    today = [NOW + timedelta(minutes=m) for m in world["stale"]]
    return max((at for at in today if start <= at < end), default=None)


@given(world=status_worlds)
def test_status_matches_db(world: dict) -> None:
    """HAT-OPS-072: for every database state, GET /api/hat/status reports each bot's mode,
    status and manage-only reasons, the state of every kill-switch key, the latest regime,
    today's broker session of every active account, today's job runs and the latest
    `feed.stale` of today, exactly as the rows hold them. Harm: the user believes the bot is
    protected or halted when it is not."""
    engine = memory_engine(*STATUS_TABLES)
    with Session(engine) as session:
        _seed_status_world(session, world)
    response = dashboard_client(engine).get(f"{API}/status")
    engine.dispose()
    assert response.status_code == 200
    body = response.json()
    kills = _expected_kills(world)
    shown_kills = {
        (k["scope"], uuid.UUID(k["bot_id"]) if k["bot_id"] else None, k["channel"]): k["state"]
        for k in body["kill_switches"]
    }
    assert shown_kills == kills
    account_tripped = TRIPPED in (
        kills.get((ACCOUNT, None, "risk")),
        kills.get((ACCOUNT, None, "health")),
    )
    assert body["kill_account"] is account_tripped
    shown_bots = {b["id"]: (b["execution_mode"], b["status"], b["reasons"]) for b in body["bots"]}
    assert shown_bots == _expected_bots(world, kills)
    assert all(b["manage_only"] is bool(b["reasons"]) for b in body["bots"])
    assert {j["job"]: j["status"] for j in body["jobs"]} == _expected_jobs(world)
    assert body["regime"] == _expected_regime(world)
    sessions = [(s["account"], s["status"]) for s in body["sessions"]]
    assert sessions == [
        (f"fyers acct{n}", login or "missing")
        for n, (active, login) in enumerate(world["accounts"])
        if active
    ]
    stale = body["feed_stale_at"]
    assert (instant(stale) if stale else None) == _expected_stale(world)
    assert body["trade_date"] == TODAY.isoformat()
