"""CONF checks of the HAT runtime's assembly (PR 17): the jobs it registers equal the 10-ops §3 job
table (HAT-OPS-020), the trigger service's route takes exactly what a `Route` is called with, and
every pull request runs the whole suite with nothing allowed to fail
(HAT-OVR-051, HAT-DEC-T4)."""

import inspect
import re
import uuid
from datetime import time
from types import SimpleNamespace

import yaml
from hat_conf_spec import REPO_ROOT, SPEC_DIR

from app.hat.clock import SYSTEM_CLOCK
from app.hat.daily.intraday import Intraday
from app.hat.daily.live import LiveRoute
from app.hat.daily.nightly import NightlyDeps
from app.hat.daily.runtime import daily_jobs
from app.hat.engine.live_entry import Route
from app.hat.execution.router import ExecutionRouter
from app.hat.ops.scheduler import Days, ScheduledJob
from app.hat.telegram.reports import Reports

ACCOUNT = "<account>"
TABLE_ROW = re.compile(
    r"^\| `([a-z_.]+(?::<account>)?)`[^|]*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|"
)
SATURDAY = 5
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED_JOBS = {
    "backend",
    "backend-postgres",
    "verification-tla",
    "verification-conformance",
    "verification-properties",
    "frontend",
    "api-schema",
}

Row = tuple[str, str, time, time | None, tuple[str, ...]]


def _clock(text: str) -> time | None:
    text = text.strip()
    return time.fromisoformat(text) if text else None


def _table() -> set[Row]:
    """The job table of 10-ops §3 as (job, days, time, until, after) rows."""
    text = (SPEC_DIR / "10-ops.md").read_text(encoding="utf-8")
    section = text[text.index("## §3 Daily cycle") : text.index("#### HAT-OPS-020")]
    rows = set()
    for line in section.splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        name, days, at, until, after = match.groups()
        rows.add(
            (
                name,
                days.strip(),
                _clock(at),
                _clock(until),
                tuple(re.findall(r"`([a-z_.]+)`", after)),
            )
        )
    return rows


def _days(job: ScheduledJob) -> str:
    days = "every" if job.days is Days.EVERY else "trading"
    return f"{days}, Saturday" if job.weekday == SATURDAY else days


def _registered() -> set[Row]:
    """The jobs `daily_jobs` registers (the runtime's own assembly) for one broker account, the
    account id replaced by `<account>`. The stand-ins are never called: only names and times are
    read."""
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
            )
        )
    )
    desks = SimpleNamespace(managers=lambda: [(account, None)])
    jobs = daily_jobs(nightly, intraday, desks, None, Reports(None, None))  # type: ignore[arg-type]
    return {
        (
            job.name.replace(str(account), ACCOUNT),
            _days(job),
            job.at,
            job.until,
            job.after,
        )
        for job in jobs
    }


def test_job_registry() -> None:
    """HAT-OPS-020: the registered (job, days, time, until, after) rows equal the job table."""
    table = _table()
    assert len(table) > 20  # the parser read the table
    registered = _registered()
    assert registered == table, {
        "not_in_table": registered - table,
        "not_registered": table - registered,
    }


def test_existing_suite_green() -> None:
    """HAT-OVR-051, HAT-DEC-T4: CI runs on every pull request, with every suite (backend,
    Postgres, the verification jobs, frontend, API schema) as a job that cannot be skipped or
    allowed to fail."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert not (triggers["pull_request"] or {}).get("branches")
    jobs = workflow["jobs"]
    assert REQUIRED_JOBS <= set(jobs)
    for name in REQUIRED_JOBS:
        job = jobs[name]
        assert "if" not in job, name
        assert not job.get("continue-on-error"), name
        for step in job.get("steps", []):
            assert not step.get("continue-on-error"), (name, step)
    runs = [step.get("run", "") for step in jobs["frontend"]["steps"]]
    assert {"pnpm check", "pnpm test", "pnpm build"} <= set(runs)


def _parameters(fn: object) -> list[tuple[str, object, object]]:
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(fn).parameters.values()  # type: ignore[arg-type]
        if p.name != "self"
    ]


def test_route_matches_protocol() -> None:
    """HAT-OPS-023, HAT-EXEC-041: `TriggerService` calls its route as a `Route`
    (`session, request, *, decide`). `ExecutionRouter.route` also needs `now` and `check`, so it
    cannot be the route; `LiveRoute` is, and its parameters equal the protocol's."""
    protocol = _parameters(Route.__call__)
    assert _parameters(LiveRoute.__call__) == protocol
    assert _parameters(ExecutionRouter.route) != protocol
