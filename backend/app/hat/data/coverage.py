"""Store coverage (DB audit H3): the stored bar and index days against the calendar's trading
days, checked nightly (`coverage.check`) so a missing session is noticed before rolling windows
and the regime's index closes silently skip it.

Three lists, each a diff of sorted day lists (`coverage_gaps`):
- `bars_missing`: a trading day from the first stored bar day through the trade date with no
  `bars_daily_raw` partition;
- `bars_extra`: a bar partition on a day the calendar does not hold as a trading day;
- `index_missing`: a trading day from the first stored index day through the trade date with no
  `index_daily` partition (the index history starts later than the bars: the days before it are
  not gaps).

The alert lists them once: it is raised when the lists differ from the last ones alerted (the
`coverage.gaps` event), and every night while a bar day is missing or extra (rerun the history
build for it). An index day that cannot be fetched (a scattered archive gap, a special session
NSE publishes no index file for) is acknowledged by the owner (`python -m app.hat.data.coverage
ack`, journalled `coverage.acknowledged`) and is never listed again.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatEvent
from ..notify import Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory
from ..time import trade_date_of, utc_now
from .calendar import session_calendar
from .store import DATASET_BARS, DATASET_INDEX, DataStore

JOB_COVERAGE = "coverage.check"
ALERT_COVERAGE = "coverage.gaps"
EVENT_GAPS = "coverage.gaps"
EVENT_ACKNOWLEDGED = "coverage.acknowledged"
# The aggregate every coverage event is journalled under: the store has no row of its own.
COVERAGE_ID = uuid.uuid5(uuid.NAMESPACE_URL, "hat:store-coverage")
ALERT_DAYS = 10  # days of each list the alert names; `report` prints them all
NOTE_MAX = 500
_KINDS = ("bars_missing", "bars_extra", "index_missing")


@dataclass(frozen=True)
class Coverage:
    bars_missing: tuple[date, ...] = ()
    bars_extra: tuple[date, ...] = ()
    index_missing: tuple[date, ...] = ()

    @property
    def count(self) -> int:
        return len(self.bars_missing) + len(self.bars_extra) + len(self.index_missing)

    @property
    def bars_wrong(self) -> bool:
        return bool(self.bars_missing or self.bars_extra)

    def without_index(self, acknowledged: Collection[date]) -> Coverage:
        """These lists less the acknowledged index days."""
        return Coverage(
            self.bars_missing,
            self.bars_extra,
            tuple(d for d in self.index_missing if d not in acknowledged),
        )

    def payload(self) -> dict[str, list[str]]:
        return {kind: [d.isoformat() for d in getattr(self, kind)] for kind in _KINDS}

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Coverage:
        def days(kind: str) -> tuple[date, ...]:
            raw = payload.get(kind)
            return tuple(date.fromisoformat(str(d)) for d in raw) if isinstance(raw, list) else ()

        return cls(*(days(kind) for kind in _KINDS))

    def alert_detail(self) -> dict[str, str | int]:
        """Each list's count and its first `ALERT_DAYS` days, one key per day (an alert value
        is one short token)."""
        detail: dict[str, str | int] = {}
        for kind in _KINDS:
            days = getattr(self, kind)
            detail[kind] = len(days)
            for i, day in enumerate(days[:ALERT_DAYS], start=1):
                detail[f"{kind}_{i:02d}"] = day.isoformat()
        return detail


def _since(days: Sequence[date], first: date | None) -> list[date]:
    return [] if first is None else [d for d in days if d >= first]


def coverage_gaps(
    trading_days: Iterable[date],
    bar_days: Iterable[date],
    index_days: Iterable[date],
    through: date,
) -> Coverage:
    """The three lists (module docstring) of the calendar's `trading_days` against the stored
    `bar_days` and `index_days`, each in date order. Trading days after `through` are not due."""
    trading = sorted(set(trading_days))
    bars, index = set(bar_days), set(index_days)
    due = [d for d in trading if d <= through]
    open_days = set(trading)
    return Coverage(
        bars_missing=tuple(d for d in _since(due, min(bars, default=None)) if d not in bars),
        bars_extra=tuple(sorted(d for d in bars if d not in open_days)),
        index_missing=tuple(d for d in _since(due, min(index, default=None)) if d not in index),
    )


def acknowledged(session: Session) -> set[date]:
    """Every index day the owner acknowledged."""
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.aggregate_id == COVERAGE_ID, HatEvent.event_type == EVENT_ACKNOWLEDGED
        )
    )
    return {d for payload in payloads for d in Coverage.from_payload(payload).index_missing}


def last_listed(session: Session) -> Coverage | None:
    """The lists the last `coverage.gaps` alert carried; None before the first."""
    payload = session.scalar(
        select(HatEvent.payload)
        .where(HatEvent.aggregate_id == COVERAGE_ID, HatEvent.event_type == EVENT_GAPS)
        .order_by(HatEvent.id.desc())
        .limit(1)
    )
    return None if payload is None else Coverage.from_payload(payload)


def pending_gaps(session: Session, store: DataStore, through: date) -> Coverage:
    """The store's gaps through `through`, less the acknowledged index days. Raises
    `JobError("coverage_no_calendar")` when the market has no calendar."""
    calendar = session_calendar(session)
    if calendar is None:
        raise JobError("coverage_no_calendar")
    found = coverage_gaps(
        calendar.trading_days(), store.days(DATASET_BARS), store.days(DATASET_INDEX), through
    )
    return found.without_index(acknowledged(session))


def coverage_body(
    *,
    session_factory: SessionFactory,
    store: DataStore,
    notifier: Notifier,
    trade_date: date,
    now: Callable[[], datetime] = utc_now,
) -> Callable[[], JobResult]:
    """The `coverage.check` body: one `coverage.gaps` alert when the lists changed since the
    last ones journalled (`coverage.gaps` holds them), and every night while a bar day is
    missing or extra. Never fails on a gap: it reports, the quality gate decides the day."""

    def body() -> JobResult:
        with session_factory() as session:
            found = pending_gaps(session, store, trade_date)
            changed = found != (last_listed(session) or Coverage())
            if changed:  # an empty list is journalled too: a gap that comes back is news
                record(
                    session,
                    aggregate_type="ingest",
                    aggregate_id=COVERAGE_ID,
                    event_type=EVENT_GAPS,
                    actor="scheduler",
                    payload={"trade_date": trade_date.isoformat(), **found.payload()},
                    occurred_at=now(),
                )
                session.commit()
        if found.count and (changed or found.bars_wrong):
            notifier.alert(ALERT_COVERAGE, trade_date, found.alert_detail())
        return JobResult(rows=found.count)

    return body


def acknowledge(
    session: Session,
    store: DataStore,
    through: date,
    *,
    note: str,
    chat_id: int,
    user_id: uuid.UUID,
    now: datetime,
) -> list[date]:
    """The owner acknowledges every index day now listed through `through` (journalled
    `coverage.acknowledged`, actor user, via cli), so no later check lists it again. Bar gaps
    are never acknowledged: the history build fixes them. Returns the days acknowledged."""
    if not note.strip() or len(note) > NOTE_MAX:
        raise ValueError("note_required")
    days = list(pending_gaps(session, store, through).index_missing)
    if days:
        record(
            session,
            aggregate_type="ingest",
            aggregate_id=COVERAGE_ID,
            event_type=EVENT_ACKNOWLEDGED,
            actor="user",
            payload={
                "chat_id": chat_id,
                "through": through.isoformat(),
                "index_missing": [d.isoformat() for d in days],
                "note": note.strip(),
            },
            user_id=user_id,
            occurred_at=now,
        )
    return days


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.coverage report [--through YYYY-MM-DD]
    python -m app.hat.data.coverage ack --note TEXT [--through YYYY-MM-DD]

    `report` prints the gaps of the store under `HAT_DATA_ROOT` through `--through` (default
    yesterday, IST), less the acknowledged index days. `ack` acknowledges every index day it
    lists (it needs `HAT_TELEGRAM_CHAT_ID`, the owner's chat id its event carries)."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.coverage")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "ack"):
        command = sub.add_parser(name)
        command.add_argument("--through", type=date.fromisoformat)
        if name == "ack":
            command.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatDataSessionLocal

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    through = args.through or trade_date_of(utc_now()) - timedelta(days=1)
    write = sys.stdout.write
    with HatDataSessionLocal() as session:
        if args.command == "report":
            found = pending_gaps(session, store, through)
            counts = {kind: len(getattr(found, kind)) for kind in _KINDS}
            write(json.dumps({"through": through.isoformat(), "counts": counts}) + "\n")
            write(json.dumps(found.payload(), indent=1) + "\n")
            return 0
        from ..errors import HatConfigError
        from ..telegram.config import owner_chat_id

        try:
            chat_id = owner_chat_id()
            days = acknowledge(
                session,
                store,
                through,
                note=args.note,
                chat_id=chat_id,
                user_id=settings.user_id,
                now=utc_now(),
            )
        except HatConfigError:
            sys.stderr.write("refused: no_owner_chat\n")
            return 2
        except ValueError as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return 2
        session.commit()
        write(json.dumps({"acknowledged": [d.isoformat() for d in days]}) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
