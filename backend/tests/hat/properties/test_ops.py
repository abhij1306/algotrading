"""PBT for the job framework and the HAT-off gate (HAT-OPS-010, -011, -013, -070)."""

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.errors import HatConfigError
from app.hat.logs import CONTEXT_KEYS, JsonFormatter
from app.hat.models import HatJobRun
from app.hat.ops.jobs import (
    DEFAULT_ERROR_CODE,
    JobError,
    JobResult,
    RunStatus,
    _claim,
    recover_interrupted,
    run_job,
)
from app.hat.ops.scheduler import start_scheduler
from app.hat.settings import load_settings
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
        lambda key: key != "HAT_ENABLED"
    ),
    st.text(max_size=8),
    max_size=5,
)


@given(raw=not_true, other=noise, dialect=st.sampled_from(["sqlite", "postgresql", "mysql", ""]))
def test_disabled_starts_nothing(raw: str | None, other: dict[str, str], dialect: str) -> None:
    """With HAT_ENABLED unset or not a true value, no scheduler, task or log handler appears,
    on any database dialect (SmartTrader's SQLite test runs stay untouched)."""
    env = dict(other)
    if raw is not None:
        env["HAT_ENABLED"] = raw
    settings_ = load_settings(env)
    assert settings_.enabled is False

    hat_logger = logging.getLogger("app.hat")
    handlers_before = list(hat_logger.handlers)

    async def start() -> tuple[object, int]:
        result = start_scheduler(settings_, dialect_name=dialect)
        return result, len(asyncio.all_tasks())

    scheduler, task_count = asyncio.run(start())
    assert scheduler is None
    assert task_count == 1  # only this coroutine itself
    assert hat_logger.handlers == handlers_before


@given(
    raw=st.sampled_from(sorted(TRUE_VALUES)).flatmap(
        lambda v: st.sampled_from([v, v.upper(), f" {v} ", v.capitalize()])
    ),
    dialect=st.text(max_size=12).filter(lambda name: name != "postgresql"),
)
def test_enabled_refuses_a_non_postgres_database(raw: str, dialect: str) -> None:
    """Enabled on any other dialect fails closed before a task or handler exists."""
    settings_ = load_settings({"HAT_ENABLED": raw})
    assert settings_.enabled is True
    hat_logger = logging.getLogger("app.hat")
    handlers_before = list(hat_logger.handlers)

    async def start() -> int:
        with pytest.raises(HatConfigError):
            start_scheduler(settings_, dialect_name=dialect)
        return len(asyncio.all_tasks())

    assert asyncio.run(start()) == 1
    assert hat_logger.handlers == handlers_before


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
        assert run_id is not None and stored is None
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
