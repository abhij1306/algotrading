"""DB checks for crash-safe routing and the approval sweep on PostgreSQL (PR 52; HAT-OPS-003,
HAT-EXEC-030, HAT-EXEC-023; audit T-F4, T-F7, T-F15).

The real router, desks and recovery over the real Fyers adapter on `FakeBroker`, committing to a
real database: a process dies right after a commit or a broker call (`crash_after`), a new one
starts on the same database (`restart`), and the sweep runs from several threads at once, each
on its own connection, so the row locks the sweep takes are the database's, not SQLite's.
Nothing reaches a real broker. The HAT tables are emptied before and after each check.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Generator
from datetime import timedelta
from decimal import Decimal

import pytest
from hat.fakes.broker import FakeBroker, accepted, rejected
from hat_router_support import (
    AT,
    CALENDAR,
    LIVE,
    TRADE_DAY,
    Crash,
    PlacementError,
    Rig,
    at_day,
    crash_after,
    desks_for,
    restart,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.hat.engine import lifecycle
from app.hat.execution import modes
from app.hat.execution.router import MAX_ENTRY_REJECTIONS
from app.hat.execution.submit import ENTRY
from app.hat.models import HatApproval, HatEvent, HatOrder, HatPosition, HatSignal

pytestmark = pytest.mark.postgres

D = Decimal
WAITING = (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL)
WORKING = ("pending", "unconfirmed", "placed", "partially_filled")


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        names = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'hat\\_%' ESCAPE '\\'")
        ).scalars()
        listed = ", ".join(f'"{name}"' for name in names)
        if listed:
            connection.execute(text(f"TRUNCATE {listed} CASCADE"))


@pytest.fixture
def clean_db(pg_engine: Engine) -> Generator[Engine, None, None]:
    """The checks commit from several sessions and threads, so they cannot run inside one
    rolled-back transaction: the HAT tables are emptied before and after."""
    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)


def _rig(session: Session, **kwargs: object) -> Rig:
    """The router rig on PostgreSQL. The shared seed writes a few rows before the rows they
    reference (universe members before their instruments, the bot before its broker account),
    which SQLite never checks: the seed runs with the foreign-key triggers off for this
    connection, and every row it leaves is consistent once it commits. The checks then run with
    them on."""
    session.execute(text("SET session_replication_role = replica"))
    rig = Rig(session, **kwargs)  # type: ignore[arg-type]
    session.execute(text("SET session_replication_role = origin"))
    session.commit()
    return rig


def _stranded(session: Session, signal_id: uuid.UUID) -> bool:
    """Waiting to be placed (auto in Triggered, or a passed Take) with no order that may work (a
    row reserved with no attempt stamped never reached the broker, so it does not count)."""
    signal = session.get(HatSignal, signal_id)
    assert signal is not None
    if signal.state not in WAITING:
        return False
    approval = session.get(HatApproval, signal_id)
    if signal.state == lifecycle.AWAITING_APPROVAL and (
        approval is None or approval.decision != "take"
    ):
        return False
    working = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.status.in_(WORKING),
            (HatOrder.status != "pending") | HatOrder.placed_at.is_not(None),
        )
    )
    return not working


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(path="auto", replies=[False, False], k=9, error="crash")
@example(path="take", replies=[True, True], k=1, error="crash")
@example(path="auto", replies=[True, True], k=2, error="raise")
@given(
    path=st.sampled_from(("auto", "take")),
    replies=st.lists(st.booleans(), min_size=2, max_size=2),
    k=st.integers(min_value=1, max_value=10),
    error=st.sampled_from(("crash", "raise")),
)
def test_db_crash_resumes_entry_once(
    clean_db: Engine, path: str, replies: list[bool], k: int, error: str
) -> None:
    """HAT-OPS-003, HAT-EXEC-023 on PostgreSQL: an auto entry or a Take whose process dies (or
    whose placement raises) right after its k-th commit or broker call is, after a restart's
    recovery and two resume passes on the same database, placed or ended exactly once: at most
    two entry orders under distinct tags, at most one the broker took, none left waiting with no
    order, and the signal's stored state is its events' replay. Harm: a restart sends a third
    entry, buys twice, or strands a signal with its slot."""
    from app.hat.engine import trades

    _truncate(clean_db)
    fake = FakeBroker()
    fake.script(
        "place_order",
        *(
            accepted(f"B-{n}") if ok else rejected("Insufficient funds")
            for n, ok in enumerate(replies)
        ),
    )
    with Session(clean_db) as session:
        rig = _rig(session, mode=modes.AUTO if path == "auto" else modes.CONFIRM, fake=fake)
        live = desks_for(clean_db, rig)
        live.recover(AT)
        rig.router = live.by_account[rig.account_id].router
        rig.guard = live.by_account[rig.account_id].guard
        signal_id = None if path == "auto" else rig.route().signal_id
        try:
            with crash_after(k, fake, Crash if error == "crash" else PlacementError):
                if path == "auto":
                    rig.route()
                else:
                    assert signal_id is not None
                    rig.router.take(
                        session,
                        signal_id,
                        price=D(rig.signal(signal_id).trigger_price),
                        locked_upper_circuit=False,
                        now=AT,
                        chat_id=7,
                        check=LIVE,
                    )
        except (Crash, PlacementError):
            session.rollback()
        signal_id = signal_id or session.scalar(select(HatSignal.id))
        assert signal_id is not None
    desks = restart(clean_db, rig, AT, None if error == "crash" else live)
    sent = rig.places()
    assert len(sent) <= MAX_ENTRY_REJECTIONS
    assert len({order["order_tag"] for order in sent}) == len(sent)
    assert len([n for n in range(len(sent)) if n >= len(replies) or replies[n]]) <= 1
    with Session(clean_db) as fresh:
        assert not _stranded(fresh, signal_id)
        state = fresh.get(HatSignal, signal_id).state  # type: ignore[union-attr]
        assert trades.replay_state(fresh, signal_id) == state
    desks.release()
    live.release()


def _snapshot(engine: Engine) -> tuple[object, ...]:
    with Session(engine) as session:
        return (
            sorted(session.execute(select(HatSignal.id, HatSignal.state)).all()),
            sorted(session.execute(select(HatApproval.signal_id, HatApproval.decision)).all()),
            sorted(
                session.execute(
                    select(HatPosition.signal_id, HatPosition.is_virtual, HatPosition.qty_open)
                ).all()
            ),
            sorted(session.execute(select(HatOrder.tag, HatOrder.status)).all()),
            session.scalar(select(func.count()).select_from(HatEvent)),
        )


def _sweep_together(engine: Engine, rig: Rig, now: object, threads: int) -> None:
    """`threads` sweeps at once, each on its own session and connection."""
    start = threading.Barrier(threads)
    errors: list[BaseException] = []

    def sweep() -> None:
        try:
            with Session(engine) as session:
                start.wait()
                rig.router.expire(session, now)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    workers = [threading.Thread(target=sweep) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []


@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(threads=4, taken=True)
@given(threads=st.integers(min_value=2, max_value=6), taken=st.booleans())
def test_db_concurrent_sweeps_settle_once(clean_db: Engine, threads: int, taken: bool) -> None:
    """HAT-EXEC-030, HAT-EXEC-032 on PostgreSQL: sweeps running at once from several threads
    over two undecided cards past their 5 minutes (and, when `taken`, one of them a Take whose
    placement crashed) leave the state one sweep leaves: each untaken card Expired with exactly
    one veto trade, and on the next trade date the stranded Take Rejected `signal_expired` with
    no order, whatever the overlap. Harm: two overlapping sweeps open two veto trades for one
    card, record its expiry twice, or end a stranded Take twice."""
    _truncate(clean_db)
    with Session(clean_db) as session:
        rig = _rig(session, mode=modes.CONFIRM)
        ids = [rig.route(instrument=n).signal_id for n in range(2)]
        if taken:
            try:
                with crash_after(1, rig.fake):
                    rig.router.take(
                        session,
                        ids[0],
                        price=D(rig.signal(ids[0]).trigger_price),
                        locked_upper_circuit=False,
                        now=AT,
                        chat_id=7,
                        check=LIVE,
                    )
            except Crash:
                session.rollback()
    late = AT + timedelta(minutes=10)
    _sweep_together(clean_db, rig, late, threads)
    once = _snapshot(clean_db)
    _sweep_together(clean_db, rig, late, threads)
    assert _snapshot(clean_db) == once
    tomorrow = CALENDAR.next_trading_day(TRADE_DAY)
    assert tomorrow is not None
    _sweep_together(clean_db, rig, at_day(tomorrow), threads)
    settled = _snapshot(clean_db)
    _sweep_together(clean_db, rig, at_day(tomorrow), threads)
    assert _snapshot(clean_db) == settled
    assert rig.places() == []
    with Session(clean_db) as session:
        for n, signal_id in enumerate(ids):
            signal = session.get(HatSignal, signal_id)
            assert signal is not None
            vetoes = session.scalars(
                select(HatPosition).where(
                    HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(True)
                )
            ).all()
            expiries = session.scalar(
                select(func.count())
                .select_from(HatEvent)
                .where(
                    HatEvent.aggregate_id == signal_id,
                    HatEvent.event_type.in_(("signal.expired", "signal.rejected")),
                )
            )
            assert expiries == 1
            if taken and n == 0:
                assert signal.state == lifecycle.REJECTED
                assert vetoes == []
            else:
                assert signal.state == lifecycle.EXPIRED
                assert len(vetoes) == 1


@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(threads=4, path="auto")
@example(threads=2, path="take")
@given(threads=st.integers(min_value=2, max_value=6), path=st.sampled_from(("auto", "take")))
def test_db_concurrent_placements_send_one(clean_db: Engine, threads: int, path: str) -> None:
    """HAT-EXEC-021, HAT-OPS-003 (PR 52) on PostgreSQL: an entry left unplaced (an auto signal
    in Triggered, or a passed Take) placed from several threads at once through the account's
    one router (the trigger path, a Take and the periodic resume share it), each on its own
    connection, reaches the broker exactly once under one tag and ends EntryPlaced. Harm: two
    placements that both read no open tag reserve two tags and buy twice."""
    _truncate(clean_db)
    with Session(clean_db) as session:
        rig = _rig(session, mode=modes.AUTO if path == "auto" else modes.CONFIRM)
        live = desks_for(clean_db, rig)
        live.recover(AT)
        router = live.by_account[rig.account_id].router
        rig.router = router
        signal_id = None if path == "auto" else rig.route().signal_id
        try:
            with crash_after(1, rig.fake):  # the commit lands, the placement never runs
                if path == "auto":
                    rig.route()
                else:
                    assert signal_id is not None
                    router.take(
                        session,
                        signal_id,
                        price=D(rig.signal(signal_id).trigger_price),
                        locked_upper_circuit=False,
                        now=AT,
                        chat_id=7,
                        check=LIVE,
                    )
        except Crash:
            session.rollback()
        signal_id = signal_id or session.scalar(select(HatSignal.id))
        assert signal_id is not None
    assert rig.places() == []
    start = threading.Barrier(threads)
    errors: list[BaseException] = []

    def place() -> None:
        try:
            with Session(clean_db) as own:
                start.wait()
                router.place_entry(own, signal_id, now=AT, check=LIVE)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    workers = [threading.Thread(target=place) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    sent = rig.places()
    assert len(sent) == 1
    with Session(clean_db) as fresh:
        assert fresh.get(HatSignal, signal_id).state == lifecycle.ENTRY_PLACED  # type: ignore[union-attr]
    live.release()
