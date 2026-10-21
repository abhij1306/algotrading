"""PBT ingest.event_and_state_atomic (HAT-DEC-I15): an ingest outcome and its event are written
by one transaction. The nightly ingest (PR 7) records `ingest.completed`, `ingest.failed` and
`quality.failed` with `events.record`; this drives the same recording with a job-run row as the
state, failing at every point before the commit."""

import uuid
from datetime import UTC, date, datetime

from hat_support import event_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.events import EventError, record
from app.hat.models import HatEvent, HatJobRun

ENGINE = event_engine(HatJobRun)

outcomes = st.sampled_from(["ingest.completed", "ingest.failed", "quality.failed"])
fail_points = st.sampled_from(["none", "after_state", "after_event", "bad_event", "rollback"])


class InjectedError(Exception):
    """A failure injected between two writes."""


@given(runs=st.lists(st.tuples(outcomes, fail_points), min_size=1, max_size=8))
def test_event_and_state_atomic(runs: list[tuple[str, str]]) -> None:
    """For every sequence of ingest outcomes, each failing at any point before its commit, the
    job-run row and the outcome event exist together or not at all."""
    committed: dict[str, str] = {}
    for number, (event_type, fail_at) in enumerate(runs):
        job = f"ingest.nightly.{uuid.uuid4().hex}"
        run_day = date(2026, 1, 1 + number)
        run_id = uuid.uuid4()
        session = Session(ENGINE)
        try:
            session.add(
                HatJobRun(
                    id=run_id,
                    job=job,
                    trade_date=run_day,
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    status="failed" if event_type != "ingest.completed" else "succeeded",
                    calls=1,
                    rows=1,
                )
            )
            session.flush()
            if fail_at == "after_state":
                raise InjectedError
            record(
                session,
                aggregate_type="ingest",
                aggregate_id=run_id,
                event_type="ingest.typo" if fail_at == "bad_event" else event_type,
                actor="scheduler",
                payload={"trade_date": str(run_day)},
            )
            if fail_at == "after_event":
                raise InjectedError
            if fail_at == "rollback":
                session.rollback()
            else:
                session.commit()
                committed[job] = event_type
        except (InjectedError, EventError):
            session.rollback()
        finally:
            session.close()
        with Session(ENGINE) as check:
            rows = check.scalar(
                select(func.count()).select_from(HatJobRun).where(HatJobRun.job == job)
            )
            events = check.scalars(
                select(HatEvent.event_type).where(HatEvent.aggregate_id == run_id)
            ).all()
        assert (rows, len(events)) == ((1, 1) if job in committed else (0, 0))
        if job in committed:
            assert events == [committed[job]]
