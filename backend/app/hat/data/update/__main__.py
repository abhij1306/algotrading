"""The daily data update's command line (docs/runbook.md, Daily data update).

    python -m app.hat.data.update [--through YYYY-MM-DD] [--only STEP ...] [--dry-run]
    python -m app.hat.data.update --status

Runs every step of `plan.STEPS` in order, each filling the sessions after its own last good one
through `--through` (default: the latest finished session). A step that fails does not stop the
steps that do not read its data; those that do are skipped. Each step's output goes to
`HAT_DATA_ROOT/logs/update/<date>/<step>.log` and its outcome to `HAT_DATA_ROOT/update/status.json`.
One run at a time (`update/update.lock`). Exit 1 when a step failed or was skipped, 3 when
another run holds the lock.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...settings import operator_settings
from ...time import IST
from .plan import (
    FAILED,
    NOT_DUE,
    OK,
    SKIPPED,
    STEP_NAMES,
    STEPS,
    Step,
    StepStatus,
    blocked_by,
    check_order,
    due,
    exit_code,
    latest_finished,
    read_status,
    selected,
    write_status,
)

if TYPE_CHECKING:
    from .steps import Context, Done

STATUS_FILE = Path("update") / "status.json"  # under the data root
LOCK_FILE = Path("update") / "update.lock"
LOG_DIR = Path("logs") / "update"
EXIT_LOCKED = 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.update", description=__doc__)
    p.add_argument("--through", type=date.fromisoformat, default=None)
    p.add_argument("--only", nargs="+", choices=STEP_NAMES, default=[], metavar="STEP")
    p.add_argument("--dry-run", action="store_true", help="say what each step would do")
    p.add_argument("--status", action="store_true", help="print the status file and exit")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data and DB
    args = _parse_args(argv)
    settings = operator_settings()  # before the database module, which reads .env too
    root = settings.data_root
    if args.status:
        path = root / STATUS_FILE
        sys.stdout.write(path.read_text() if path.exists() else "no status yet\n")
        return exit_code({n: s.result for n, s in read_status(path).items()})
    from .. import minute_history

    check_order(STEPS)
    try:
        with minute_history.run_lock(root / LOCK_FILE):
            return _run(args, root, settings.duckdb_memory)
    except minute_history.MinuteHistoryError:
        sys.stderr.write("update_already_running\n")
        return EXIT_LOCKED


def _run(args: argparse.Namespace, root: Path, memory: str | None) -> int:  # pragma: no cover
    from app.database import HatDataSessionLocal

    from ..calendar import session_calendar
    from .steps import BODIES, Context

    now = datetime.now(IST)
    with HatDataSessionLocal() as session:
        calendar = session_calendar(session)
    sessions = calendar.trading_days() if calendar is not None else []
    through = args.through or latest_finished(sessions, now.replace(tzinfo=None))
    if through is None:
        sys.stderr.write("update_no_calendar\n")
        return 2
    status_path = root / STATUS_FILE
    status = read_status(status_path)
    results: dict[str, str] = {}
    sys.stdout.write(f"data update through {through}{' (dry run)' if args.dry_run else ''}\n")
    for step in selected(STEPS, args.only):
        log = root / LOG_DIR / now.date().isoformat() / f"{step.name}.log"
        ctx = Context(root, sessions, through, now.date(), log, args.dry_run, memory)
        forced = step.name in args.only  # a step named by hand runs whatever its cadence
        entry = _step(step, status.get(step.name), results, ctx, BODIES[step.name], forced)
        results[step.name] = entry.result
        sys.stdout.write(f"{step.name:<13} {entry.result:<8} {entry.last_good or '-':<11} ")
        sys.stdout.write(f"{entry.detail}\n")
        sys.stdout.flush()
        if not args.dry_run:
            status[step.name] = entry
            write_status(status_path, status, _run_record(now, through, results))
    return exit_code(results)


def _step(  # noqa: PLR0913
    step: Step,
    previous: StepStatus | None,
    results: Mapping[str, str],
    ctx: Context,
    body: Callable[[Context], Done],
    forced: bool,
) -> StepStatus:
    """Run one step (or skip it), its status entry as the file keeps it."""
    started = datetime.now(IST).isoformat(timespec="seconds")
    kept = previous.last_good if previous is not None else None
    last_ok = previous.last_ok_run if previous is not None else None
    blocker = blocked_by(step, results)
    if blocker is not None:
        return StepStatus(SKIPPED, started, kept, last_ok, f"{blocker} did not succeed")
    if not (ctx.dry_run or forced) and not due(step.cadence, _day(last_ok), ctx.today):
        entry = previous or StepStatus(NOT_DUE, started)
        return replace(entry, result=NOT_DUE, detail=f"weekly, last ok {last_ok}")
    from .steps import StepError

    if not ctx.dry_run:
        ctx.log.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(ctx.log) if not ctx.dry_run else None
    root_logger = logging.getLogger()
    if handler is not None:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    try:
        done = body(ctx)
    except StepError as exc:
        good = exc.last_good.isoformat() if exc.last_good else kept
        return StepStatus(FAILED, started, good, last_ok, exc.detail, str(ctx.log))
    except Exception as exc:  # noqa: BLE001 - one step's bug must not stop the others
        logging.getLogger(__name__).error("step %s crashed\n%s", step.name, traceback.format_exc())
        return StepStatus(FAILED, started, kept, last_ok, type(exc).__name__, str(ctx.log))
    finally:
        if handler is not None:
            root_logger.removeHandler(handler)
            handler.close()
    good = done.last_good.isoformat() if done.last_good else kept
    ok_on = last_ok if ctx.dry_run else ctx.today.isoformat()
    return StepStatus(OK, started, good, ok_on, done.detail, str(ctx.log))


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def _run_record(now: datetime, through: date, results: dict[str, str]) -> dict[str, object]:
    return {
        "started": now.isoformat(timespec="seconds"),
        "through": through.isoformat(),
        "failed": sorted(n for n, r in results.items() if r == FAILED),
        "skipped": sorted(n for n, r in results.items() if r == SKIPPED),
        "ok": sorted(n for n, r in results.items() if r in (OK, NOT_DUE)),
    }


if __name__ == "__main__":
    sys.exit(main())
