"""CONF checks of the HAT runtime's assembly: the trigger service's route takes exactly what a
`Route` is called with, and every pull request runs the whole suite with nothing allowed to fail
(HAT-OVR-051, HAT-DEC-T4)."""

import inspect

import yaml
from hat_conf_spec import REPO_ROOT

from app.hat.daily.live import LiveRoute
from app.hat.engine.live_entry import Route
from app.hat.execution.router import ExecutionRouter

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
