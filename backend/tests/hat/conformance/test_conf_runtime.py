"""CONF check of the CI workflow: every pull request runs every suite with nothing allowed to
fail."""

import yaml
from hat_conf_repo import REPO_ROOT

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
    """CI runs on every pull request, with every suite (backend,
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
