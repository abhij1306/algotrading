"""The out-of-process watch (runbook, Health watch): what the runtime cannot tell about itself.

`python -m app.hat.ops.watch`, run every five minutes by a systemd user timer beside the backend
(`deploy/systemd/hat-watch.*`), looks at:

- the daily data update's status file (`update.failed`: the run's failed or skipped steps;
  `update.late`: no run started today by `UPDATE_LATE_AT`), when this machine runs it;
- the data verification reports under `audits/verify-*` (`verify.failed`: the newest report
  that ran a check failed it);
- the free space of the data root's and the root file system (`disk.low`);
- both databases (`db.down`: no answer to `SELECT 1`);
- the runtime's scheduler heartbeat (`runtime.down`: no beat for `HEARTBEAT_STALE`, so the
  process is dead or wedged, or the scheduler loop stopped), with `HAT_ENABLED` only, and only
  while hat_state answers (a database that is down is its own finding).

Each finding has a key naming its event; it is alerted once while it stands, through the same
`render.alert_text` as the runtime's alerts (with what to do), and told resolved when it clears.
The open keys live in `HAT_DATA_ROOT/ops/watch.json`; an alert that could not be sent stays
unopened and is tried again at the next run. `--dry-run` prints the findings and sends and writes
nothing. The rules are the pure functions here; the reads and the send are the thin rest.
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
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..time import IST

__all__ = [
    "DISK_MIN_FREE_BYTES",
    "DISK_MIN_FREE_SHARE",
    "HEARTBEAT_STALE",
    "UPDATE_LATE_AT",
    "Finding",
    "Transition",
    "disk_findings",
    "heartbeat_findings",
    "transition",
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
# The timer starts the update at 19:30 IST; a day with no run started by this is late.
UPDATE_LATE_AT = time(21, 30)
DISK_MIN_FREE_BYTES = 10 * 1024**3
DISK_MIN_FREE_SHARE = 0.10
# The scheduler beats every 30 s (`ops/scheduler.TICK_SECONDS`): six missed beats is down.
HEARTBEAT_STALE = timedelta(minutes=3)
STATE_FILE = Path("ops") / "watch.json"  # under the data root
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


def _numbered(prefix: str, names: Iterable[str]) -> dict[str, str]:
    return {f"{prefix}.{n}": name for n, name in enumerate(sorted(names), start=1)}


def update_findings(status: Mapping[str, Any] | None, now: datetime) -> list[Finding]:
    """The data update's findings from its status file (None: this machine has none, so it does
    not run the update): its latest run's failed and skipped steps, one alert per run and set of
    steps (a step that fails later in the same run is told with the others); and a day with no
    run started by `UPDATE_LATE_AT` IST."""
    if status is None:
        return []
    out: list[Finding] = []
    run = status.get("run") or {}
    started = str(run.get("started") or "")
    failed = [str(s) for s in run.get("failed") or ()]
    skipped = [str(s) for s in run.get("skipped") or ()]
    if failed or skipped:
        steps = ",".join(sorted(failed) + sorted(skipped))
        out.append(
            Finding(
                UPDATE_FAILED,
                f"{UPDATE_FAILED}:{started}:{steps}",
                {**_numbered("failed", failed), **_numbered("skipped", skipped)},
            )
        )
    local = now.astimezone(IST)
    try:
        last = datetime.fromisoformat(started).astimezone(IST).date() if started else None
    except ValueError:
        last = None
    if local.time() >= UPDATE_LATE_AT and (last is None or last < local.date()):
        detail: dict[str, str | int] = {"last_run": last.isoformat() if last else "never"}
        out.append(Finding(UPDATE_LATE, f"{UPDATE_LATE}:{local.date().isoformat()}", detail))
    return out


def verify_findings(reports: Sequence[tuple[str, Mapping[str, Any]]]) -> list[Finding]:
    """For each check (D1..D7), the newest of the `(folder name, verify.json)` reports that ran
    it (by `created`; a skipped check did not run): `verify.failed` when that one failed it, one
    alert per check and report."""
    newest: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
    for name, doc in reports:
        created = str(doc.get("created") or "")
        for check, result in (doc.get("checks") or {}).items():
            if not isinstance(result, Mapping) or result.get("status") == "skipped":
                continue
            if check not in newest or created > newest[check][0]:
                newest[check] = (created, name, doc)
    out = []
    for check, (_, name, doc) in sorted(newest.items()):
        if doc["checks"][check].get("status") == _FAIL:
            digest = str(doc.get("digest") or name)
            out.append(
                Finding(
                    VERIFY_FAILED,
                    f"{VERIFY_FAILED}:{check}:{digest}",
                    {"check": str(check), "report": name},
                )
            )
    return out


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


def _answers(name: str | None) -> bool:
    """Whether the database `name` (None: hat_live) answers `SELECT 1`."""
    from sqlalchemy import text

    from app.database import create_engine_from_env

    engine = create_engine_from_env(name, pool_size=1)
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

    from app.database import create_engine_from_env, state_database_name

    from ..models import HatHeartbeat
    from .scheduler import HEARTBEAT

    engine = create_engine_from_env(state_database_name(), pool_size=1)
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
    """Every finding of this run, in a fixed order."""
    from app.database import state_database_name

    found = update_findings(_status(root), now)
    found += verify_findings(_reports(root))
    found += disk_findings(_usage({"data_root": root, "root": Path("/")}))
    live, state = _answers(None), _answers(state_database_name())
    found += [
        Finding(DB_DOWN, f"{DB_DOWN}:{label}", {"database": label})
        for label, up in (("live", live), ("state", state))
        if not up
    ]
    if runtime and state:
        found += heartbeat_findings(_last_beat(), now)
    return found


# --- the send --------------------------------------------------------------------------------


Send = Callable[[str], bool]


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


def tell(
    open_: Mapping[str, Mapping[str, Any]],
    found: Sequence[Finding],
    now: datetime,
    send: Send,
) -> dict[str, dict[str, Any]]:
    """Send the new findings' alerts and the cleared keys' resolved notes; return the open keys
    after it. A new finding whose alert was not sent stays unopened, so the next run tries it
    again; a resolved note that was not sent is not tried again."""
    from ..telegram import render

    day = now.astimezone(IST).date()
    step = transition(open_, found)
    after = {key: dict(entry) for key, entry in open_.items()}
    for key, entry in step.cleared:
        send(render.resolved_text(str(entry.get("code", key)), day))
        after.pop(key, None)
    for f in step.new:
        if send(render.alert_text(f.code, day, f.detail)):
            after[f.key] = {"code": f.code, "since": now.isoformat(timespec="seconds")}
    return after


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.ops.watch", description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="print the findings; send nothing")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real files, DB, chat
    args = _parse_args(argv)
    from ..settings import is_enabled, operator_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = operator_settings()
    root = settings.data_root
    now = datetime.now(IST)
    found = findings(root, now, runtime=is_enabled())
    for f in found:
        sys.stdout.write(f"{f.code} {f.key} {json.dumps(dict(f.detail), sort_keys=True)}\n")
    if args.dry_run:
        sys.stdout.write(f"{len(found)} finding(s); dry run: nothing sent\n")
        return 0
    path = root / STATE_FILE
    state = _read_json(path)
    open_ = state.get("open", {}) if isinstance(state, Mapping) else {}
    after = tell(open_, found, now, _telegram())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"open": after}, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    sys.stdout.write(f"{len(found)} finding(s), {len(after)} open\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
