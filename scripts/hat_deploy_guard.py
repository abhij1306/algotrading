"""Deploy-window guard: refuse a deploy or planned restart during market hours (HAT-OPS-042).

Usage (from the repo root), as the first step of every deploy (`docs/runbook.md`):
    python scripts/hat_deploy_guard.py && docker compose ... up -d --build
    python scripts/hat_deploy_guard.py --force --reason "hotfix: GTT swap crash"

Exit code 0 means the deploy may go ahead; 2 means it is refused. The window is 08:30 to 15:35
IST (start included, end excluded) on trading days. `--force` lets a deploy through inside the
window only with a `--reason`, and every forced run is printed and appended as one JSON line to
the guard log, `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl` (fixed: no argument names a file to write).

Trading days. The guard runs on the host before the stack is up, so it cannot read
`hat_trading_calendar`. It fails closed: every weekday counts as a trading day. `--calendar`
takes an export of that table (`date,is_trading` per line, as `psql -At -F,` prints it) and its
rows win over the weekday rule, so an exchange holiday is free and a special weekend session is
guarded.

`deploy_allowed` holds the rule with no IO, so the property `ops.deploy_window_guard` checks it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
WINDOW_START = time(8, 30)
WINDOW_END = time(15, 35)
REFUSED = 2
TRUE_FLAGS = {"t", "true", "1", "yes", "y"}
DEFAULT_DATA_ROOT = Path("data_system") / "hat"


def in_market_window(now: datetime) -> bool:
    """`now` (timezone-aware) falls in 08:30 <= t < 15:35 IST."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return WINDOW_START <= now.astimezone(IST).time() < WINDOW_END


def deploy_allowed(now: datetime, trading_day: bool, force: bool) -> bool:
    """A deploy may run unless it is a trading day inside the window and not forced."""
    return force or not (trading_day and in_market_window(now))


def is_trading_day(day: date, calendar: Mapping[date, bool]) -> bool:
    """The calendar's row for `day`, else the fail-closed rule: every weekday trades."""
    return calendar.get(day, day.weekday() < 5)


def parse_calendar(lines: Iterable[str]) -> dict[date, bool]:
    """`date,is_trading` lines; blank lines and `#` comments are skipped."""
    calendar = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        day, _, flag = line.partition(",")
        calendar[date.fromisoformat(day.strip())] = flag.strip().lower() in TRUE_FLAGS
    return calendar


def _default_log() -> Path:
    root = os.environ.get("HAT_DATA_ROOT", "").strip()
    return (Path(root) if root else DEFAULT_DATA_ROOT) / "ops" / "deploy_guard.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="deploy inside the window")
    parser.add_argument("--reason", default="", help="why a forced deploy cannot wait")
    parser.add_argument("--calendar", type=Path, help="hat_trading_calendar export (date,flag)")
    args = parser.parse_args(argv)
    if args.force and not args.reason.strip():
        parser.error("--force needs --reason")
    return args


def log_forced(now: datetime, reason: str) -> None:
    record = {
        "ts": now.astimezone(UTC).isoformat(),
        "ist": now.astimezone(IST).isoformat(),
        "event": "deploy.forced",
        "reason": reason,
        "user": os.environ.get("USER", ""),
    }
    line = json.dumps(record, sort_keys=True)
    print(f"FORCED deploy inside the market window: {line}", file=sys.stderr)
    path = _default_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    args = parse_args(argv)
    now = now or datetime.now(UTC)
    calendar = {}
    if args.calendar is not None:
        calendar = parse_calendar(args.calendar.read_text(encoding="utf-8").splitlines())
    trading = is_trading_day(now.astimezone(IST).date(), calendar)
    if not deploy_allowed(now, trading, force=False):
        if not args.force:
            print(
                "REFUSED: no deploys or restarts between 08:30 and 15:35 IST on a trading day "
                f"(now {now.astimezone(IST):%Y-%m-%d %H:%M} IST). Wait, or use --force --reason.",
                file=sys.stderr,
            )
            return REFUSED
        log_forced(now, args.reason)
    print(f"deploy allowed ({now.astimezone(IST):%Y-%m-%d %H:%M} IST)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
