"""The daily data update's plan, without IO: the steps and their order, which sessions a step
still has to fill, when a weekly step is due, and the status file's shape (docs/runbook.md,
Daily data update)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

DAILY = "daily"
WEEKLY = "weekly"
WEEK = timedelta(days=7)
# A session counts as finished once its bhavcopy is due: the nightly ingest's first probe
# (`ingest.PROBE_START`, 18:00 IST); before that the latest finished session is the one before.
READY_AT = time(18, 0)

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"  # a step it waits on failed or was skipped in this run
NOT_DUE = "not_due"  # a weekly step run within the week
RESULTS = (OK, FAILED, SKIPPED, NOT_DUE)


@dataclass(frozen=True)
class Step:
    name: str
    after: tuple[str, ...] = ()  # the steps whose data it reads; it is skipped when one fails
    cadence: str = DAILY
    about: str = ""


# Every daily data step, in the order they run (each after the steps it waits on).
STEPS: tuple[Step, ...] = (
    Step("calendar", about="NSE holiday list into hat_trading_calendar (calendar.refresh)"),
    Step("bars", about="bhavcopy days into bars_daily_raw, index_daily (history_cli --resume)"),
    Step("listing", ("bars",), about="NSE equity list into the stock instrument master"),
    Step(
        "ingest",
        ("bars",),
        about="per session: index levels, market breadth, NIFTY 500 TRI, ingest.completed",
    ),
    Step("quality", ("ingest",), about="per session: quality.gate"),
    Step("ca", ("ingest",), about="per session: ca.adjust (bc file, dividends, demergers)"),
    Step("features", ("quality", "ca"), about="features_daily and breadth_daily"),
    Step("membership", ("bars",), about="NIFTY 500 membership CSV rebuilt and loaded"),
    Step(
        "bars5m",
        ("bars", "membership"),
        about="Kotak 5-minute top-up of the members' missing sessions, imported",
    ),
    Step("etf", about="ETF overlay data top-up, built and stored under etf/"),
    Step("results", cadence=WEEKLY, about="NSE results fetch, parse, derive"),
    Step("fundamentals", ("results", "bars"), WEEKLY, "fundamentals extract and derive"),
    # last, after whatever ran: its failure never stops a data step
    Step("record", about="store manifests and reference CSVs pushed to the data repo"),
)
STEP_NAMES = tuple(s.name for s in STEPS)


def check_order(steps: Sequence[Step]) -> None:
    """Raise ValueError unless every step waits only on known steps listed before it."""
    seen: set[str] = set()
    for step in steps:
        unknown = [a for a in step.after if a not in seen]
        if unknown or step.name in seen:
            raise ValueError(f"step {step.name} out of order: {unknown or 'duplicate'}")
        seen.add(step.name)


def selected(steps: Sequence[Step], only: Iterable[str] = ()) -> list[Step]:
    """The steps to run, in table order: all of them, or those named in `only` (the steps they
    wait on are then not run; they read what is stored). ValueError for an unknown name."""
    wanted = set(only)
    unknown = wanted - {s.name for s in steps}
    if unknown:
        raise ValueError(f"unknown step: {', '.join(sorted(unknown))}")
    return [s for s in steps if not wanted or s.name in wanted]


def blocked_by(step: Step, results: Mapping[str, str]) -> str | None:
    """The first step `step` waits on that failed or was skipped in this run, else None."""
    return next((a for a in step.after if results.get(a) in (FAILED, SKIPPED)), None)


def latest_finished(trading_days: Sequence[date], now: datetime) -> date | None:
    """The latest session whose data is due by `now` (an IST wall time): today from `READY_AT`
    when today trades, else the last trading day before today. None when there is none."""
    today = now.date()
    ready = today if now.time() >= READY_AT else today - timedelta(days=1)
    return max((d for d in trading_days if d <= ready), default=None)


def sessions_to_fill(
    trading_days: Sequence[date], last_good: date | None, through: date
) -> list[date]:
    """The sessions after `last_good` up to `through`, oldest first. A step with no good session
    yet starts at `through` alone: the history before it is the backfill tools' (runbook)."""
    if last_good is None:
        return [through] if through in trading_days else []
    return sorted(d for d in trading_days if last_good < d <= through)


def recent_sessions(
    trading_days: Sequence[date], last_stored: date | None, through: date, lookback: int
) -> list[date]:
    """The sessions a per-instrument check covers: the last `lookback` through `through`, and
    every one after `last_stored` (a long stop), oldest first."""
    upto = sorted(d for d in trading_days if d <= through)
    recent = upto[-lookback:] if lookback > 0 else []
    later = [d for d in upto if last_stored is None or d > last_stored]
    return sorted(set(recent) | set(later)) if last_stored is not None else recent


def gaps(
    expected: Mapping[date, set[str]],
    stored: Mapping[date, set[str]],
    explained: set[tuple[str, date]] = frozenset(),
) -> dict[date, set[str]]:
    """Per session, the expected instruments with no stored bar and no ledgered refusal; only
    the sessions with any."""
    out = {}
    for day, want in expected.items():
        missing = {i for i in want - stored.get(day, set()) if (i, day) not in explained}
        if missing:
            out[day] = missing
    return out


def due(cadence: str, last_ok: date | None, today: date) -> bool:
    """Whether a step runs today: a daily one always, a weekly one a week after its last success."""
    return cadence == DAILY or last_ok is None or today - last_ok >= WEEK


@dataclass(frozen=True)
class StepStatus:
    result: str
    last_run: str  # ISO time the step last ran
    last_good: str | None = None  # the session its data is complete through
    last_ok_run: str | None = None  # the IST date it last succeeded (a weekly step's clock)
    detail: str = ""
    log: str = ""


def read_status(path: Path) -> dict[str, StepStatus]:
    """The status file's steps; empty when there is no file yet."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {name: StepStatus(**fields) for name, fields in raw.get("steps", {}).items()}


def write_status(path: Path, steps: Mapping[str, StepStatus], run: Mapping[str, object]) -> None:
    """Write the status file whole (a temporary file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"run": dict(run), "steps": {n: asdict(s) for n, s in sorted(steps.items())}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def exit_code(results: Mapping[str, str]) -> int:
    """1 when any step failed or was skipped, else 0."""
    return 1 if any(r in (FAILED, SKIPPED) for r in results.values()) else 0
