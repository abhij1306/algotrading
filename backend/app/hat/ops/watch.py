"""The out-of-process watch (runbook, Health watch): what the runtime cannot tell about itself.

`python -m app.hat.ops.watch`, run every five minutes by a systemd user timer beside the backend
(`deploy/systemd/hat-watch.*`), looks at:

- the daily data update's status file (`update.failed`: the latest run's failed or skipped
  steps; `update.late`: no run started for the latest due run date, `UPDATE_LATE_AT` IST), when
  this machine runs it;
- the data verification reports under `audits/verify-*` (`verify.failed`: the newest report
  that ran a check failed it);
- the free space of the data root's and the root file system (`disk.low`);
- both databases (`db.down`: no answer to `SELECT 1`, hat_live read-only);
- the runtime's scheduler heartbeat (`runtime.down`: no beat for `HEARTBEAT_STALE`, so the
  process is dead or wedged, or the scheduler loop stopped), with `HAT_ENABLED` only, and only
  while hat_state answers (a database that is down is its own finding).

A check that raises is its own finding (`watch.check_error`) and the others still run. Each
finding has a key naming its event; it is alerted once while it stands, through the same
`render.alert_text` as the runtime's alerts (with what to do), and told resolved, with its
subject, when it clears. The open keys live in `HAT_DATA_ROOT/ops/watch.json`, written
atomically before anything is sent (`run_watch`): a state that cannot be written sends nothing
but one `watch.state_unwritable` alert, so a full disk never repeats the open alerts every run;
when hat_state cannot record that alert either, the run exits 1 so the `OnFailure=` unit tells
it (`unwritable_exit`).
A run that fails outright sends one `watch.failed` and exits 1, so the unit's `OnFailure=` unit
(`hat-watch-failed`, started at most once a day) tells it when even that could not. The
once-a-time alerts are claimed as `hat_heartbeat` rows (`Marks`), so they hold across runs
without the state file. `--dry-run` prints the findings and sends and writes nothing. The rules
are the pure functions here; the reads and the sends are the thin rest.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..time import IST

__all__ = [
    "CHECK_ERROR",
    "DISK_MIN_FREE_BYTES",
    "DISK_MIN_FREE_SHARE",
    "HEARTBEAT_STALE",
    "STATE_UNWRITABLE",
    "UPDATE_LATE_AT",
    "WATCH_FAILED",
    "Finding",
    "Marks",
    "Transition",
    "WatchRun",
    "checked",
    "disk_findings",
    "heartbeat_findings",
    "run_watch",
    "save_state",
    "transition",
    "unwritable_exit",
    "update_due",
    "update_findings",
    "verify_findings",
]

logger = logging.getLogger(__name__)

UPDATE_FAILED = "update.failed"
UPDATE_LATE = "update.late"
VERIFY_FAILED = "verify.failed"
DISK_LOW = "disk.low"
DB_DOWN = "db.down"
RUNTIME_DOWN = "runtime.down"
CHECK_ERROR = "watch.check_error"
STATE_UNWRITABLE = "watch.state_unwritable"
WATCH_FAILED = "watch.failed"
# The timer starts the update at 19:30 IST; a run date with no run started by this is late.
UPDATE_LATE_AT = time(21, 30)
DISK_MIN_FREE_BYTES = 10 * 1024**3
DISK_MIN_FREE_SHARE = 0.10
# The scheduler beats every 30 s (`ops/scheduler.TICK_SECONDS`): six missed beats is down.
HEARTBEAT_STALE = timedelta(minutes=3)
STATE_FILE = Path("ops") / "watch.json"  # under the data root
# A server that takes the connection and then stops answering fails the check, not the run.
_TIMEOUT = "-c statement_timeout=5000"
STATUS_FILE = Path("update") / "status.json"
VERIFY_GLOB = "audits/verify-*/verify.json"
_FAIL = "fail"


@dataclass(frozen=True)
class Finding:
    """One thing wrong now: its alert `code`, the `key` of its event (one alert per key while
    it stands) and the alert's detail (fixed codes and ids only, as every alert's)."""

    code: str
    key: str
    detail: Mapping[str, str | int] = field(default_factory=dict)


# --- the rules -------------------------------------------------------------------------------


def _names(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _numbered(prefix: str, names: Iterable[str]) -> dict[str, str]:
    return {f"{prefix}.{n}": name for n, name in enumerate(sorted(names), start=1)}


def update_due(now: datetime) -> date:
    """The latest run date whose update should have started by `now`: today from
    `UPDATE_LATE_AT` IST, else yesterday (so the window does not end at midnight)."""
    local = now.astimezone(IST)
    return local.date() if local.time() >= UPDATE_LATE_AT else local.date() - timedelta(days=1)


def update_findings(status: Mapping[str, Any] | None, now: datetime) -> list[Finding]:
    """The data update's findings from its status file (None: this machine has none, so it does
    not run the update): its latest run's failed and skipped steps, one alert per run (keyed by
    its start; a step that fails later in the run is not a second alert); and no run started on
    or after the latest due run date (`update_due`), one alert per due date."""
    if status is None:
        return []
    out: list[Finding] = []
    run = status.get("run")
    run = run if isinstance(run, Mapping) else {}
    started = str(run.get("started") or "")
    failed, skipped = _names(run.get("failed")), _names(run.get("skipped"))
    if failed or skipped:
        out.append(
            Finding(
                UPDATE_FAILED,
                f"{UPDATE_FAILED}:{started}",
                {**_numbered("failed", failed), **_numbered("skipped", skipped)},
            )
        )
    try:
        last = datetime.fromisoformat(started).astimezone(IST).date() if started else None
    except ValueError:
        last = None
    due = update_due(now)
    if last is None or last < due:
        detail: dict[str, str | int] = {
            "due": due.isoformat(),
            "last_run": last.isoformat() if last else "never",
        }
        out.append(Finding(UPDATE_LATE, f"{UPDATE_LATE}:{due.isoformat()}", detail))
    return out


def verify_findings(reports: Sequence[tuple[str, Mapping[str, Any]]]) -> list[Finding]:
    """For each check (D1..D7), the newest of the `(folder name, verify.json)` reports that ran
    it (by `created`; a skipped check did not run): `verify.failed` when that one failed it, one
    alert per check and report."""
    newest: dict[str, tuple[str, str, str]] = {}  # check -> (created, folder, status)
    for name, doc in reports:
        created = str(doc.get("created") or "")
        checks = doc.get("checks")
        for check, result in checks.items() if isinstance(checks, Mapping) else ():
            status = result.get("status") if isinstance(result, Mapping) else None
            if status in (None, "skipped"):
                continue
            if check not in newest or created > newest[check][0]:
                newest[check] = (created, name, str(status))
    digests = {name: str(doc.get("digest") or name) for name, doc in reports}
    return [
        Finding(
            VERIFY_FAILED,
            f"{VERIFY_FAILED}:{check}:{digests[name]}",
            {"check": str(check), "report": name},
        )
        for check, (_, name, status) in sorted(newest.items())
        if status == _FAIL
    ]


def disk_findings(
    usage: Mapping[str, tuple[int, int]],
    min_free: int = DISK_MIN_FREE_BYTES,
    min_share: float = DISK_MIN_FREE_SHARE,
) -> list[Finding]:
    """`disk.low` for each labelled file system whose `(free, total)` bytes are below
    `min_free` or `min_share` of its size."""
    out = []
    for label, (free, total) in sorted(usage.items()):
        if free < min_free or (total > 0 and free / total < min_share):
            share = int(free * 100 / total) if total > 0 else 0
            detail: dict[str, str | int] = {
                "disk": label,
                "free_gb": free // 1024**3,
                "free_pct": share,
            }
            out.append(Finding(DISK_LOW, f"{DISK_LOW}:{label}", detail))
    return out


def heartbeat_findings(
    beat_at: datetime | None, now: datetime, stale: timedelta = HEARTBEAT_STALE
) -> list[Finding]:
    """`runtime.down` when the scheduler's last beat (None: never) is older than `stale`."""
    if beat_at is not None and now - beat_at <= stale:
        return []
    since = beat_at.astimezone(IST).strftime("%Y-%m-%dT%H:%M") if beat_at else "never"
    return [Finding(RUNTIME_DOWN, RUNTIME_DOWN, {"last_beat": since})]


def checked(name: str, check: Callable[[], list[Finding]]) -> list[Finding]:
    """`check`'s findings, or, when it raises, one `watch.check_error` naming it (the other
    checks still run)."""
    try:
        return check()
    except Exception as exc:
        logger.exception("watch check %s failed", name)
        detail: dict[str, str | int] = {"check": name, "error": type(exc).__name__}
        return [Finding(CHECK_ERROR, f"{CHECK_ERROR}:{name}", detail)]


@dataclass(frozen=True)
class Transition:
    """What a run tells: the findings newly found and the open keys that cleared."""

    new: list[Finding]
    cleared: list[tuple[str, Mapping[str, Any]]]  # (key, its open entry)


def transition(open_: Mapping[str, Mapping[str, Any]], found: Sequence[Finding]) -> Transition:
    """Against the keys alerted and still open (`open_`, key -> its entry, holding `code`),
    the findings of this run: each new key is told, each open key no finding has any more is
    told resolved, and a key still found is told nothing (one alert per event). Findings that
    share a key are one event: the first is kept."""
    seen: dict[str, Finding] = {}
    for f in found:
        seen.setdefault(f.key, f)
    new = [f for key, f in seen.items() if key not in open_]
    cleared = [(key, entry) for key, entry in open_.items() if key not in seen]
    return Transition(new, cleared)


Send = Callable[[str], bool]
Save = Callable[[Mapping[str, Mapping[str, Any]]], bool]


class Marks(Protocol):
    """Once-a-time claims that hold across runs without the state file (`hat_heartbeat`)."""

    def claim(self, name: str) -> bool | None:
        """True when `name` was not claimed and now is; False when it already was; None when
        the claim could not be read or written."""
        ...

    def clear(self, name: str) -> None: ...


def _entry(f: Finding, now: datetime) -> dict[str, Any]:
    return {"code": f.code, "detail": dict(f.detail), "since": now.isoformat(timespec="seconds")}


def unwritable_exit(claimed: bool | None) -> int:
    """The run's exit code when its state could not be written, by the `watch.state_unwritable`
    claim: 1 when the claim could not be read either (hat_state down too), so nothing could be
    told or recorded and the unit's `OnFailure=` (`hat-watch-failed`, at most once a day) tells
    the owner; else 0 (the one alert went out, or already had)."""
    return 1 if claimed is None else 0


@dataclass(frozen=True)
class WatchRun:
    """A run's outcome: the open keys after it (None: the state could not be written) and the
    process's exit code."""

    open: dict[str, dict[str, Any]] | None
    exit_code: int = 0


def run_watch(
    open_: Mapping[str, Mapping[str, Any]],
    found: Sequence[Finding],
    now: datetime,
    send: Send,
    save: Save,
    marks: Marks,
) -> WatchRun:
    """Tell this run's findings against the open keys and return the open keys after it (None
    when the state could not be written, with `unwritable_exit`'s code). The state that would
    follow (the cleared keys gone, the new ones open) is saved before anything is sent: when
    that fails, no finding is told and one `watch.state_unwritable` goes out, claimed in `marks`
    until a save works again, so no run repeats alerts it cannot record. A new alert that was
    not delivered is taken out again by a second save, so the next run tries it; should that
    save fail, it waits as told. A resolved note that was not sent is not tried again."""
    from ..telegram import render

    day = now.astimezone(IST).date()
    step = transition(open_, found)
    cleared = {key for key, _ in step.cleared}
    after = {k: dict(v) for k, v in open_.items() if k not in cleared}
    after.update({f.key: _entry(f, now) for f in step.new})
    if not save(after):
        claimed = marks.claim(STATE_UNWRITABLE)
        if claimed:
            text = render.alert_text(STATE_UNWRITABLE, day, {"file": STATE_FILE.as_posix()})
            if not send(text):
                marks.clear(STATE_UNWRITABLE)
        return WatchRun(None, unwritable_exit(claimed))
    marks.clear(STATE_UNWRITABLE)
    for _, entry in step.cleared:
        detail = entry.get("detail")
        subject = detail if isinstance(detail, Mapping) else {}
        send(render.resolved_text(str(entry.get("code", "")), day, subject))
    unsent = [f.key for f in step.new if not send(render.alert_text(f.code, day, f.detail))]
    if unsent:
        for key in unsent:
            after.pop(key, None)
        save(after)
    return WatchRun(after)


def save_state(path: Path, open_: Mapping[str, Mapping[str, Any]]) -> bool:
    """Write the open keys to `path` atomically (a temporary file renamed over it); False when
    it cannot be written (a full disk, a broken folder), with nothing half-written left."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"open": open_}, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error("watch state %s not written: %s", path, type(exc).__name__)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("watch state temporary file %s left behind", tmp)
        return False


# --- the reads -------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:  # noqa: ANN401 - a JSON document
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _status(root: Path) -> Mapping[str, Any] | None:
    path = root / STATUS_FILE
    if not path.exists():
        return None
    doc = _read_json(path)
    # an unreadable status file is a run that left no record: late, not silent
    return doc if isinstance(doc, Mapping) else {}


def _reports(root: Path) -> list[tuple[str, Mapping[str, Any]]]:
    out = []
    for path in sorted(root.glob(VERIFY_GLOB)):
        doc = _read_json(path)
        if isinstance(doc, Mapping):
            out.append((path.parent.name, doc))
    return out


def _usage(paths: Mapping[str, Path]) -> dict[str, tuple[int, int]]:
    """Free and total bytes per labelled path, one label per file system."""
    out: dict[str, tuple[int, int]] = {}
    devices: set[int] = set()
    for label, path in paths.items():
        try:
            device = path.stat().st_dev
            if device in devices:
                continue
            devices.add(device)
            disk = shutil.disk_usage(path)
        except OSError:
            continue
        out[label] = (disk.free, disk.total)
    return out


def _engine(database: str | None) -> Any:  # noqa: ANN401 - a SQLAlchemy engine
    """A one-connection engine on hat_live (None: read-only, as every reader's) or on
    `database`, each statement under a timeout."""
    from app.database import create_engine_from_env

    from ..storage import live_connect_options

    options = _TIMEOUT
    if database is None:
        options = f"{live_connect_options(writer=False)} {_TIMEOUT}".strip()
    return create_engine_from_env(database, options=options, pool_size=1)


def _answers(name: str | None) -> bool:
    """Whether the database `name` (None: hat_live) answers `SELECT 1`."""
    from sqlalchemy import text

    engine = _engine(name)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # the text may carry the server's: only the type is kept
        logger.warning("database check failed: %s", type(exc).__name__)
        return False
    finally:
        engine.dispose()


def _last_beat() -> datetime | None:
    """The scheduler's last beat; None when it never beat (or before the runtime made the
    table)."""
    from sqlalchemy import select

    from app.database import state_database_name

    from ..models import HatHeartbeat
    from .scheduler import HEARTBEAT

    engine = _engine(state_database_name())
    try:
        with engine.connect() as connection:
            return connection.execute(
                select(HatHeartbeat.beat_at).where(HatHeartbeat.name == HEARTBEAT)
            ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("heartbeat unreadable: %s", type(exc).__name__)
        return None
    finally:
        engine.dispose()


def findings(root: Path, now: datetime, runtime: bool) -> list[Finding]:
    """Every finding of this run, in a fixed order; each check on its own (`checked`)."""
    from app.database import state_database_name

    answers = {"live": False, "state": False}

    def databases() -> list[Finding]:
        answers.update(live=_answers(None), state=_answers(state_database_name()))
        return [
            Finding(DB_DOWN, f"{DB_DOWN}:{label}", {"database": label})
            for label, up in answers.items()
            if not up
        ]

    found = checked("update", lambda: update_findings(_status(root), now))
    found += checked("verify", lambda: verify_findings(_reports(root)))
    found += checked("disk", lambda: disk_findings(_usage({"data_root": root, "root": Path("/")})))
    found += checked("databases", databases)
    if runtime and answers["state"]:
        found += checked("heartbeat", lambda: heartbeat_findings(_last_beat(), now))
    return found


# --- the sends and the marks -----------------------------------------------------------------


def _telegram() -> Send:
    """A send to the allowlisted chat, or to the log when the Telegram settings are not set."""
    from ..errors import HatConfigError
    from ..telegram.client import BotClient, HttpsTransport, TelegramError
    from ..telegram.config import telegram_settings

    try:
        settings = telegram_settings()
    except HatConfigError as exc:
        logger.warning("%s: the watch's alerts go to the log", exc)

        def to_log(text: str) -> bool:
            logger.warning("watch alert\n%s", text)
            return True

        return to_log
    client = BotClient(HttpsTransport(settings.token), settings.chat_id)

    def to_chat(text: str) -> bool:
        try:
            client.send(text)
        except TelegramError as exc:
            logger.warning("watch alert not sent: %s", exc.code)
            return False
        return True

    return to_chat


class HeartbeatMarks:
    """`Marks` as `hat_heartbeat` rows named `watch:<name>` (stamped when claimed)."""

    def claim(self, name: str) -> bool | None:
        from sqlalchemy.orm import Session

        from app.database import state_database_name

        from ..models import HatHeartbeat
        from ..time import utc_now

        engine = _engine(state_database_name())
        try:
            with Session(engine) as session:
                if session.get(HatHeartbeat, f"watch:{name}") is not None:
                    return False
                session.add(HatHeartbeat(name=f"watch:{name}", beat_at=utc_now()))
                session.commit()
                return True
        except Exception as exc:
            logger.warning("watch mark %s unreadable: %s", name, type(exc).__name__)
            return None
        finally:
            engine.dispose()

    def clear(self, name: str) -> None:
        from sqlalchemy import delete

        from app.database import state_database_name

        from ..models import HatHeartbeat

        engine = _engine(state_database_name())
        try:
            with engine.begin() as connection:
                connection.execute(delete(HatHeartbeat).where(HatHeartbeat.name == f"watch:{name}"))
        except Exception as exc:
            logger.warning("watch mark %s not cleared: %s", name, type(exc).__name__)
        finally:
            engine.dispose()


def _failed_once(error: str, *, unknown_sends: bool) -> None:
    """One `watch.failed` per failure spell (claimed in `HeartbeatMarks`; a run that completes
    clears it). When the claim cannot be read it is sent only if `unknown_sends` (the
    `OnFailure=` unit, which systemd starts at most once a day)."""
    from ..telegram import render

    marks = HeartbeatMarks()
    claimed = marks.claim(WATCH_FAILED)
    if claimed is False or (claimed is None and not unknown_sends):
        return
    day = datetime.now(IST).date()
    if not _telegram()(render.alert_text(WATCH_FAILED, day, {"error": error})) and claimed:
        marks.clear(WATCH_FAILED)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.ops.watch", description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="print the findings; send nothing")
    p.add_argument(
        "--notify-failure", action="store_true", help="the OnFailure= unit: tell the run failed"
    )
    return p.parse_args(argv)


def _run(args: argparse.Namespace) -> int:  # pragma: no cover - real files, DB, chat
    from ..settings import is_enabled, operator_settings

    root = operator_settings().data_root
    now = datetime.now(IST)
    found = findings(root, now, runtime=is_enabled())
    for f in found:
        sys.stdout.write(f"{f.code} {f.key} {json.dumps(dict(f.detail), sort_keys=True)}\n")
    if args.dry_run:
        sys.stdout.write(f"{len(found)} finding(s); dry run: nothing sent\n")
        return 0
    path = root / STATE_FILE
    state = _read_json(path)
    open_ = state.get("open") if isinstance(state, Mapping) else None
    marks = HeartbeatMarks()
    outcome = run_watch(
        open_ if isinstance(open_, Mapping) else {},
        found,
        now,
        _telegram(),
        lambda doc: save_state(path, doc),
        marks,
    )
    if outcome.open is None:
        sys.stdout.write(f"{len(found)} finding(s); state unwritable: nothing told\n")
        return outcome.exit_code  # 1: the OnFailure= unit tells it (`unwritable_exit`)
    marks.clear(WATCH_FAILED)
    sys.stdout.write(f"{len(found)} finding(s), {len(outcome.open)} open\n")
    return outcome.exit_code


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real files, DB, chat
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.notify_failure:
        from ..settings import operator_settings

        operator_settings()
        _failed_once("unit_failed", unknown_sends=True)
        return 0
    try:
        return _run(args)
    except Exception as exc:
        logger.exception("watch run failed")
        if not args.dry_run:
            try:
                _failed_once(type(exc).__name__, unknown_sends=False)
            except Exception:
                logger.exception("watch.failed not sent")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
