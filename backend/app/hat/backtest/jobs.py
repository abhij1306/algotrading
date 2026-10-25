"""Backtest jobs started from the app (`POST /api/hat/backtests`): one research run or a small
sweep, run by the backtest command line (`python -m app.hat.backtest`) in a child process so
the market load and the day loop never share the server's memory, threads or event loop.

A job is a research run of a strategy version on a universe (never a promotion run: the
command line gets no `--for-promotion`). One variant runs as a plain run, its overrides as
`--set`, `--risk-pct`, `--max-positions`, `--sleeve-weight` and `--slot-order`; two or more run
as a sweep from a variants file (`runs.parse_variants`), which the command line commits and
prints one JSON line per variant as each finishes. The job's progress is the variants printed
so far; its runs land in `hat_backtest_run` like any other.

Its files live under `HAT_DATA_ROOT/backtests/jobs/<job id>/`: `variants.json` (a sweep's),
`out.jsonl` (the command line's stdout) and `err.log` (its stderr, for the operator; never sent
to the app). One job runs at a time. The jobs are kept in memory only: after a server restart
the list starts empty, and a child still running finishes on its own and records its runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.paths import PROJECT_ROOT

from ..time import utc_now
from .overrides import OverrideError, parse_set
from .slots import SLOT_ORDER_TIME
from .spec import BacktestError, RunConventions

__all__ = [
    "JOB_BUSY",
    "MAX_VARIANTS",
    "Job",
    "JobVariant",
    "Jobs",
    "check_variants",
    "command",
    "outcomes_of",
    "refusal_of",
    "set_text",
    "variants_doc",
]

logger = logging.getLogger(__name__)

JOB_BUSY = "backtest_job_running"
JOB_FAILED = "backtest_job_failed"
MAX_VARIANTS = 8
KEEP_JOBS = 20
RUNNING, COMPLETED, FAILED, REFUSED = "running", "completed", "failed", "refused"
_REFUSED = re.compile(r"backtest refused: ([a-z0-9_]+)", re.ASCII)


@dataclass(frozen=True)
class JobVariant:
    """One run of a job: its label (a sweep's) and its overrides, each None for the version's
    or the policy's own value; `sets` are (path, JSON value) pairs (`overrides.parse_set`)."""

    label: str
    sets: tuple[tuple[str, Any], ...] = ()
    risk_pct: Decimal | None = None
    max_positions: int | None = None
    sleeve_weight: Decimal | None = None
    slot_order: str | None = None


def set_text(path: str, value: object) -> str:
    """One override as the command line takes it: `path=<value as JSON>`."""
    return f"{path}={json.dumps(value)}"


def _decimal(number: Decimal) -> str:
    return format(number.normalize(), "f")


def variants_doc(variants: Sequence[JobVariant]) -> list[dict[str, Any]]:
    """A sweep's variants file (`runs.parse_variants`): each variant's label and only the
    overrides it sets."""
    doc = []
    for v in variants:
        item: dict[str, Any] = {"label": v.label}
        if v.sets:
            item["set"] = [set_text(path, value) for path, value in v.sets]
        if v.risk_pct is not None:
            item["risk_pct"] = _decimal(v.risk_pct)
        if v.max_positions is not None:
            item["max_positions"] = v.max_positions
        if v.sleeve_weight is not None:
            item["sleeve_weight"] = _decimal(v.sleeve_weight)
        if v.slot_order is not None:
            item["slot_order"] = v.slot_order
        doc.append(item)
    return doc


def check_variants(variants: Sequence[JobVariant]) -> None:
    """Refuse, before the market loads, what the command line would refuse in the variants file
    (`runs.parse_variants`, which the API may not import: it reaches the engine): 1 to
    `MAX_VARIANTS` variants, unique non-empty labels, malformed override paths, and sleeve
    weights and slot orders `RunConventions` refuses (`BacktestError` with its code)."""
    labels = [v.label for v in variants]
    if not 1 <= len(variants) <= MAX_VARIANTS or not all(labels) or len(set(labels)) != len(labels):
        raise BacktestError("backtest_sweep_bad_variants")
    for v in variants:
        try:
            for path, value in v.sets:
                parse_set(set_text(path, value))
        except OverrideError as exc:
            raise BacktestError(exc.code) from None
        RunConventions(slot_order=v.slot_order or SLOT_ORDER_TIME, sleeve_weight=v.sleeve_weight)


def command(
    version_id: uuid.UUID,
    universe: str,
    start: date,
    end: date,
    variants: Sequence[JobVariant],
    variants_file: Path,
) -> list[str]:
    """The command line's arguments (after `python -m app.hat.backtest`) for a job: a research
    run (`--strategy-version`, `--universe`), its one variant's overrides as flags, or a sweep
    from `variants_file`. Never `--for-promotion`."""
    args = [
        "--strategy-version",
        str(version_id),
        "--universe",
        universe,
        "--from",
        start.isoformat(),
        "--to",
        end.isoformat(),
    ]
    if len(variants) > 1:
        return [*args, "--variants", str(variants_file)]
    (v,) = variants
    for path, value in v.sets:
        args += ["--set", set_text(path, value)]
    if v.risk_pct is not None:
        args += ["--risk-pct", _decimal(v.risk_pct)]
    if v.max_positions is not None:
        args += ["--max-positions", str(v.max_positions)]
    if v.sleeve_weight is not None:
        args += ["--sleeve-weight", _decimal(v.sleeve_weight)]
    if v.slot_order is not None:
        args += ["--slot-order", v.slot_order]
    return args


def outcomes_of(text: str, sweep: bool) -> list[dict[str, Any]]:
    """The finished variants in the command line's stdout, each `{label, run_id, status, code}`:
    a sweep's one JSON line per variant (a partial last line skipped), or a plain run's one JSON
    summary once it is complete (none before)."""
    keys = ("label", "run_id", "status", "code")
    if not sweep:
        try:
            doc = json.loads(text)
        except ValueError:
            return []
        if not isinstance(doc, dict) or "run_id" not in doc:
            return []
        return [{"label": None, "run_id": doc["run_id"], "status": doc.get("status"), "code": None}]
    found = []
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            found.append({k: doc.get(k) for k in keys})
    return found


def refusal_of(stderr: str) -> str | None:
    """The fixed code of a run the command line refused (`backtest refused: <code>`), if any."""
    found = _REFUSED.findall(stderr)
    return found[-1] if found else None


@dataclass
class Job:
    """One started job and what is known of it; `outcomes` and `status` change as it runs."""

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    universe: str
    start: date
    end: date
    labels: tuple[str, ...]
    sweep: bool
    folder: Path
    started_at: datetime
    status: str = RUNNING
    code: str | None = None
    finished_at: datetime | None = None
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    def _read(self, name: str) -> str:
        try:
            return (self.folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def refresh(self) -> None:
        """Re-read the finished variants from the child's stdout."""
        self.outcomes = outcomes_of(self._read("out.jsonl"), self.sweep)

    def finish(self, returncode: int) -> None:
        """Record how the child ended: refused (exit 2 with a code), failed or completed."""
        self.refresh()
        self.finished_at = utc_now()
        if returncode == 0:
            self.status = COMPLETED
            return
        code = refusal_of(self._read("err.log"))
        self.status = REFUSED if code is not None else FAILED
        self.code = code or JOB_FAILED


class Jobs:
    """The app's backtest jobs, newest first, one running at a time (module docstring)."""

    def __init__(self, python: str = sys.executable) -> None:
        self._python = python
        self._lock = threading.Lock()
        self._jobs: list[Job] = []

    def list(self) -> list[Job]:
        with self._lock:
            for job in self._jobs:
                if job.status == RUNNING:
                    job.refresh()
            return list(self._jobs)

    def start(
        self,
        root: Path,
        version_id: uuid.UUID,
        universe: str,
        start: date,
        end: date,
        variants: Sequence[JobVariant],
    ) -> Job:
        """Start the job's child process and a thread that waits for it; `BacktestError`
        `JOB_BUSY` while another job runs."""
        with self._lock:
            if any(job.status == RUNNING for job in self._jobs):
                raise BacktestError(JOB_BUSY)
            job_id = uuid.uuid4()
            folder = root / "backtests" / "jobs" / str(job_id)
            folder.mkdir(parents=True)
            variants_file = folder / "variants.json"
            if len(variants) > 1:
                variants_file.write_text(json.dumps(variants_doc(variants)), encoding="utf-8")
            args = command(version_id, universe, start, end, variants, variants_file)
            job = Job(
                id=job_id,
                strategy_version_id=version_id,
                universe=universe,
                start=start,
                end=end,
                labels=tuple(v.label for v in variants),
                sweep=len(variants) > 1,
                folder=folder,
                started_at=utc_now(),
            )
            with (
                (folder / "out.jsonl").open("wb") as out,
                (folder / "err.log").open("wb") as err,
            ):
                child = subprocess.Popen(  # noqa: S603 - our own interpreter and module, no shell
                    [self._python, "-m", "app.hat.backtest", *args],
                    cwd=PROJECT_ROOT / "backend",
                    env={**os.environ, "HAT_DATA_ROOT": str(root)},
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            self._jobs.insert(0, job)
            del self._jobs[KEEP_JOBS:]
        logger.info("HAT backtest job %s started: %d variant(s)", job_id, len(variants))
        threading.Thread(
            target=self._wait, args=(job, child), name=f"hat-backtest-{job_id}", daemon=True
        ).start()
        return job

    def _wait(self, job: Job, child: subprocess.Popen[bytes]) -> None:
        returncode = child.wait()
        with self._lock:
            job.finish(returncode)
        logger.info("HAT backtest job %s ended: %s (%s)", job.id, job.status, job.code)
