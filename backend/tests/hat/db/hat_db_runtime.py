"""Database rules of the runtime tables and the event log (03-schema §5, §6, §8), each as a check
body over a Postgres connection. Same contract as `hat_db_rules.py`: a check raises
AssertionError when the database does not enforce its rule, and `test_db_broken_variants.py`
requires each to fail once the trigger, index or constraint is removed (HAT-SCH-102)."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import hat_db_builders as b
from hat_db_rules import accepts, rejects
from sqlalchemy import Connection, Engine, delete, select, text, update
from sqlalchemy.exc import DBAPIError

from app.hat import models as m
from app.hat.ids import uuid7
from app.hat.ops.partitions import (
    ensure_event_partitions,
    month_of,
    months_to_cover,
    partition_bounds,
    partition_name,
)
from app.hat.schema import ensure_hat_schema

# --- runtime tables (HAT-SCH-051 .. HAT-SCH-058) -------------------------------------------


def check_runtime__signal_unique_per_day(conn: Connection) -> None:
    w = b.world(conn)
    b.signal(conn, w)
    rejects(conn, "a second signal with the same key", lambda: b.signal(conn, w, qty=1))
    rejects(
        conn,
        "a second signal with the same key and other mode and price",
        lambda: b.signal(conn, w, mode="auto", trigger_price=Decimal("1")),
    )
    accepts(conn, "another playbook", lambda: b.signal(conn, w, playbook="breakout"))
    accepts(conn, "another day", lambda: b.signal(conn, w, trade_date=date(2026, 10, 21)))
    other = b.instrument(conn, w["market"], isin="INE000A01099", symbol="ZZZ")
    accepts(conn, "another instrument", lambda: b.signal(conn, w, instrument_id=other))
    strategy = b.strategy(conn, w["market"])
    other_bot = b.bot(conn, strategy, w["account"])
    accepts(conn, "another bot", lambda: b.signal(conn, w, bot_id=other_bot))


def check_runtime__signal_unique_concurrent(engine: Engine) -> None:
    """Two transactions insert the same signal key at once: exactly one commits."""
    committed: dict[str, uuid.UUID] = {}
    with engine.begin() as connection:
        committed = b.world(connection)
    insert_sql = text(
        "INSERT INTO hat_signal (id, bot_id, strategy_version_id, risk_policy_version_id,"
        " playbook, instrument_id, trade_date, triggered_at, trigger_price, stop_price, qty,"
        " risk_amount, currency, mode, state, risk_decision) VALUES (:id, :bot, :version,"
        " :policy, 'race', :instrument, :day, now(), 100, 95, 1, 5, 'INR', 'shadow',"
        " 'triggered', '{}')"
    )
    outcomes: list[str] = []
    first_in = threading.Event()
    release = threading.Event()

    def worker(hold: bool) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if not hold:
                    first_in.wait(5)
                params = {**committed, "id": uuid7(), "day": b.TRADE_DAY}
                connection.execute(insert_sql, params)
                if hold:
                    first_in.set()
                    release.wait(10)
                transaction.commit()
                outcomes.append("committed")
            except DBAPIError:
                transaction.rollback()
                outcomes.append("rejected")

    holder = threading.Thread(target=worker, args=(True,))
    racer = threading.Thread(target=worker, args=(False,))
    try:
        holder.start()
        racer.start()
        deadline = time.monotonic() + 5
        waiting = text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
        while time.monotonic() < deadline:
            with engine.connect() as probe:
                if probe.execute(waiting).scalar_one() >= 1:
                    break
            time.sleep(0.05)  # noqa: ASYNC251
        release.set()
        holder.join(10)
        racer.join(10)
        assert sorted(outcomes) == ["committed", "rejected"], outcomes
    finally:
        release.set()
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM hat_signal WHERE playbook = 'race'"))


def check_runtime__signal_versions_not_null(conn: Connection) -> None:
    w = b.world(conn)
    rejects(
        conn,
        "a signal with no strategy version",
        lambda: b.signal(conn, w, strategy_version_id=None),
    )
    rejects(
        conn,
        "a signal with no risk policy version",
        lambda: b.signal(conn, w, risk_policy_version_id=None),
    )
    rejects(
        conn,
        "a signal with a dangling strategy version",
        lambda: b.signal(conn, w, strategy_version_id=uuid.uuid4()),
    )
    rejects(
        conn,
        "a signal with a dangling risk policy version",
        lambda: b.signal(conn, w, risk_policy_version_id=uuid.uuid4()),
    )
    accepts(conn, "a signal naming both versions", lambda: b.signal(conn, w))


def _rejects_each(
    conn: Connection, what: str, values: tuple[object, ...], write: Callable[[object], object]
) -> None:
    for value in values:
        rejects(conn, f"{what} {value!r}", lambda v=value: write(v))


def check_runtime__domain_checks(conn: Connection) -> None:
    w = b.world(conn)
    day = [date(2026, 11, 1)]

    def new_signal(**values: object) -> uuid.UUID:
        day[0] += timedelta(days=1)
        return b.signal(conn, w, trade_date=day[0], **values)

    signal = new_signal()
    for purpose in ("entry", "stop_day", "stop_gtt", "exit_partial", "exit_full", "flatten"):
        accepts(
            conn, f"order purpose {purpose}", lambda p=purpose: b.order(conn, w, signal, purpose=p)
        )
    _rejects_each(
        conn,
        "order purpose",
        ("", "Entry", "ENTRY", "exit", "stop", "buy", "gtt"),
        lambda v: b.order(conn, w, signal, purpose=v),
    )
    for side in ("buy", "sell"):
        accepts(conn, f"order side {side}", lambda s=side: b.order(conn, w, signal, side=s))
    _rejects_each(
        conn,
        "order side",
        ("", "Buy", "SELL", "short", "b"),
        lambda v: b.order(conn, w, signal, side=v),
    )
    _rejects_each(conn, "order qty", (0, -1), lambda v: b.order(conn, w, signal, qty=v))

    approval = {
        "sent_at": b.NOW,
        "expires_at": b.later(1),
        "token_hash": "h",
    }
    for decision in (None, "take", "skip", "expired"):
        accepts(
            conn,
            f"approval decision {decision}",
            lambda d=decision: b.add(
                conn, m.HatApproval, signal_id=new_signal(), decision=d, **approval
            ),
        )
    _rejects_each(
        conn,
        "approval decision",
        ("", "Take", "approved", "SKIP", "expire"),
        lambda v: b.add(conn, m.HatApproval, signal_id=new_signal(), decision=v, **approval),
    )

    for mode in ("shadow", "confirm", "auto"):
        accepts(conn, f"signal mode {mode}", lambda md=mode: new_signal(mode=md))
    _rejects_each(
        conn, "signal mode", ("", "Shadow", "live", "paper"), lambda v: new_signal(mode=v)
    )
    accepts(conn, "a signal with qty 0", lambda: new_signal(qty=0))
    _rejects_each(conn, "signal qty", (-1, -100), lambda v: new_signal(qty=v))

    order = b.order(conn, w, signal)
    first = order
    _rejects_each(
        conn,
        "fill qty",
        (0, -5),
        lambda v: b.fill(conn, first, qty=v, broker_trade_id=f"t{v}"),
    )
    accepts(conn, "a fill with qty 1", lambda: b.fill(conn, order, qty=1, broker_trade_id="ok"))
    _rejects_each(
        conn,
        "position qty_open",
        (-1, -10),
        lambda v: b.position(conn, w, new_signal(), qty_open=v),
    )
    accepts(
        conn,
        "a position with qty_open 0",
        lambda: b.position(conn, w, new_signal(), qty_open=0),
    )
    rejects(
        conn,
        "a real fill with no broker trade id",
        lambda: b.fill(conn, first, is_virtual=False, broker_trade_id=None),
    )
    accepts(conn, "a virtual fill with no broker trade id", lambda: b.fill(conn, first))
    rejects(
        conn,
        "a position opened with a stop below its initial stop",
        lambda: b.position(
            conn, w, new_signal(), initial_stop=Decimal("95"), current_stop=Decimal("94")
        ),
    )
    accepts(
        conn,
        "a position opened with a stop above its initial stop",
        lambda: b.position(
            conn, w, new_signal(), initial_stop=Decimal("95"), current_stop=Decimal("96")
        ),
    )


ORDER_STATUSES = (
    "pending",
    "placed",
    "unconfirmed",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
)


def check_runtime__order_status_check(conn: Connection) -> None:
    w = b.world(conn)
    signal = b.signal(conn, w)
    for status in ORDER_STATUSES:
        accepts(conn, f"order status {status}", lambda s=status: b.order(conn, w, signal, status=s))
    _rejects_each(
        conn,
        "order status",
        ("", "PENDING", "Pending", "open", "complete", "submitted", "partial"),
        lambda v: b.order(conn, w, signal, status=v),
    )
    # A status can also not be changed to a value outside the set.
    order_id = b.order(conn, w, signal)
    table = m.HatOrder.__table__
    rejects(
        conn,
        "updating an order to an unknown status",
        lambda: conn.execute(update(table).where(table.c.id == order_id).values(status="open")),
    )


def check_runtime__order_tag_unique(conn: Connection) -> None:
    w = b.world(conn)
    signal = b.signal(conn, w)
    b.order(conn, w, signal, tag="HT00000000000000000A")
    rejects(
        conn,
        "a second order with the same tag",
        lambda: b.order(conn, w, signal, tag="HT00000000000000000A", purpose="stop_day"),
    )
    other = b.signal(conn, w, playbook="other")
    rejects(
        conn,
        "the same tag on another signal",
        lambda: b.order(conn, w, other, tag="HT00000000000000000A"),
    )
    accepts(conn, "another tag", lambda: b.order(conn, w, signal, tag="HT00000000000000000B"))


def check_runtime__fill_unique(conn: Connection) -> None:
    w = b.world(conn)
    signal = b.signal(conn, w)
    first = b.order(conn, w, signal, is_virtual=False)
    second = b.order(conn, w, signal, is_virtual=False, purpose="exit_full", side="sell")
    b.fill(conn, first, broker_trade_id="T1", is_virtual=False)
    rejects(
        conn,
        "the same broker trade twice for an order",
        lambda: b.fill(conn, first, broker_trade_id="T1", is_virtual=False, qty=3),
    )
    accepts(
        conn,
        "another broker trade for the order",
        lambda: b.fill(conn, first, broker_trade_id="T2", is_virtual=False),
    )
    accepts(
        conn,
        "the same broker trade id on another order",
        lambda: b.fill(conn, second, broker_trade_id="T1", is_virtual=False),
    )


def check_runtime__trade_result_once(conn: Connection) -> None:
    w = b.world(conn)
    first = b.position(conn, w, b.signal(conn, w))
    second = b.position(conn, w, b.signal(conn, w, playbook="two"))
    b.trade_result(conn, first)
    rejects(
        conn,
        "a second trade result for one position",
        lambda: b.trade_result(conn, first, realized_pnl=Decimal("-9")),
    )
    accepts(conn, "a result for another position", lambda: b.trade_result(conn, second))


def check_ops__snapshot_unique(conn: Connection) -> None:
    w = b.world(conn)
    b.snapshot(conn, w["bot"], b.TRADE_DAY)
    rejects(
        conn,
        "a second snapshot for one bot and day",
        lambda: b.snapshot(conn, w["bot"], b.TRADE_DAY, equity=Decimal("1")),
    )
    accepts(
        conn, "another day", lambda: b.snapshot(conn, w["bot"], b.TRADE_DAY + timedelta(days=1))
    )
    other = b.bot(conn, b.strategy(conn, w["market"]), w["account"])
    accepts(conn, "another bot", lambda: b.snapshot(conn, other, b.TRADE_DAY))
    for level in ("L1", "L2", "L3"):
        accepts(
            conn,
            f"exposure level {level}",
            lambda lv=level: b.snapshot(
                conn, other, b.TRADE_DAY + timedelta(days=2 + int(lv[1])), exposure_level=lv
            ),
        )
    _rejects_each(
        conn,
        "exposure level",
        ("", "L0", "L4", "l1"),
        lambda v: b.snapshot(conn, other, b.TRADE_DAY + timedelta(days=30), exposure_level=v),
    )


# --- invariant 1 (HAT-SCH-081) --------------------------------------------------------------

STOPS = (
    Decimal("0.0001"),
    Decimal("94.9999"),
    Decimal("95"),
    Decimal("95.0001"),
    Decimal("100"),
    Decimal("1234.5678"),
    Decimal("99999999999999.9999"),
)


def check_governance__stop_only_rises(conn: Connection) -> None:
    w = b.world(conn)
    table = m.HatPosition.__table__
    for old in STOPS:
        for new in STOPS:
            position = b.position(
                conn,
                w,
                b.signal(conn, w, playbook=f"p{old}-{new}"),
                current_stop=old,
                # Far below every tested stop, so the trigger (not the insert CHECK) decides.
                initial_stop=Decimal("0.0001"),
            )

            def move(p: uuid.UUID = position, n: Decimal = new) -> None:
                conn.execute(update(table).where(table.c.id == p).values(current_stop=n))

            if new < old:
                rejects(conn, f"lowering the stop from {old} to {new}", move)
                kept = conn.execute(select(table.c.current_stop).where(table.c.id == position))
                assert kept.scalar_one() == old, "a refused stop move changed the stop"
            else:
                accepts(conn, f"keeping or raising the stop from {old} to {new}", move)
                kept = conn.execute(select(table.c.current_stop).where(table.c.id == position))
                assert kept.scalar_one() == new
    # Lowering together with other columns is still refused, and other columns move freely.
    position = b.position(conn, w, b.signal(conn, w, playbook="combo"), current_stop=Decimal("100"))
    rejects(
        conn,
        "lowering the stop in an update that also changes the state",
        lambda: conn.execute(
            update(table)
            .where(table.c.id == position)
            .values(current_stop=Decimal("99"), state="trailing")
        ),
    )
    accepts(
        conn,
        "an update that does not touch the stop",
        lambda: conn.execute(
            update(table).where(table.c.id == position).values(state="trailing", qty_open=5)
        ),
    )
    rejects(
        conn,
        "lowering every stop with one statement",
        lambda: conn.execute(update(table).values(current_stop=Decimal("0.0001"))),
    )


# --- event log (HAT-SCH-060, 061, 066, 068) -------------------------------------------------

APPEND_ONLY_MESSAGE = "hat_event is append-only"


def rejects_append_only(conn: Connection, what: str, action: Callable[[], object]) -> None:
    """`action` must raise the append-only error (not just any error)."""
    savepoint = conn.begin_nested()
    try:
        action()
    except DBAPIError as exc:
        savepoint.rollback()
        assert APPEND_ONLY_MESSAGE in str(exc), f"{what} was refused for another reason: {exc}"
        return
    savepoint.rollback()
    raise AssertionError(f"the database accepted: {what}")


EVENT_UPDATES: dict[str, object] = {
    "occurred_at": b.later(3),
    "user_id": uuid.uuid4(),
    "bot_id": uuid.uuid4(),
    "aggregate_type": "order",
    "aggregate_id": uuid.uuid4(),
    "event_type": "order.placed",
    "actor": "user",
    "correlation_id": uuid.uuid4(),
    "causation_id": 1,
    "payload": {"changed": True},
    "recorded_at": b.NOW + timedelta(days=2),
}


def check_events__append_only(conn: Connection) -> None:
    ensure_event_partitions(conn, datetime.now(UTC).date())
    table = m.HatEvent.__table__
    now = datetime.now(UTC)
    ids = [b.event(conn), b.event(conn, recorded_at=now + timedelta(days=40))]
    before = {row.id: tuple(row) for row in conn.execute(select(table).order_by(table.c.id)).all()}
    for event_id in ids:
        for column, value in EVENT_UPDATES.items():
            rejects_append_only(
                conn,
                f"updating {column} of an event",
                lambda c=column, v=value, i=event_id: conn.execute(
                    update(table).where(table.c.id == i).values(**{c: v})
                ),
            )
        rejects_append_only(
            conn,
            "updating every column of an event",
            lambda i=event_id: conn.execute(
                update(table).where(table.c.id == i).values(**EVENT_UPDATES)
            ),
        )
        rejects_append_only(
            conn,
            "deleting an event",
            lambda i=event_id: conn.execute(delete(table).where(table.c.id == i)),
        )
    rejects_append_only(conn, "deleting every event", lambda: conn.execute(delete(table)))
    rejects_append_only(
        conn,
        "updating every event",
        lambda: conn.execute(update(table).values(payload={"x": 1})),
    )
    after = {row.id: tuple(row) for row in conn.execute(select(table).order_by(table.c.id)).all()}
    assert after == before, "a refused statement changed the event log"
    accepts(conn, "appending another event", lambda: b.event(conn))


def check_events__partitioned(conn: Connection) -> None:
    kind = conn.execute(
        text(
            "SELECT partstrat, (SELECT attname FROM pg_attribute WHERE attrelid = partrelid"
            " AND attnum = partattrs[0]) FROM pg_partitioned_table"
            " WHERE partrelid = to_regclass('hat_event')"
        )
    ).one_or_none()
    assert kind is not None, "hat_event is not a partitioned table"
    assert tuple(kind) == ("r", "recorded_at"), f"hat_event is partitioned as {tuple(kind)}"
    columns = conn.execute(
        text(
            "SELECT a.attname, a.attidentity FROM pg_attribute a"
            " WHERE a.attrelid = to_regclass('hat_event') AND a.attnum > 0 AND NOT a.attisdropped"
            " ORDER BY a.attnum"
        )
    ).all()
    assert [c[0] for c in columns] == [
        "id",
        "recorded_at",
        "occurred_at",
        "user_id",
        "bot_id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "actor",
        "correlation_id",
        "causation_id",
        "payload",
    ]
    assert dict(columns)["id"] == "a", "hat_event.id is not GENERATED ALWAYS AS IDENTITY"
    key = conn.execute(
        text(
            "SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_constraint c"
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)"
            " WHERE c.conrelid = to_regclass('hat_event') AND c.contype = 'p'"
        )
    ).scalar_one()
    assert key == ["id", "recorded_at"], f"primary key is {key}"
    ensure_event_partitions(conn, datetime.now(UTC).date())
    rejects(
        conn,
        "an event by an unknown actor",
        lambda: b.event(conn, actor="admin"),
    )
    for actor in ("engine", "user", "broker", "scheduler"):
        accepts(conn, f"actor {actor}", lambda a=actor: b.event(conn, actor=a))
    ids = [b.event(conn) for _ in range(20)]
    assert ids == sorted(set(ids)), "event ids do not strictly increase"


def _children(conn: Connection) -> dict[str, str]:
    rows = conn.execute(
        text(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i"
            " JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = to_regclass('hat_event')"
        )
    ).all()
    return dict(tuple(row) for row in rows)


def _routed_to(conn: Connection, instant: datetime) -> str:
    event_id = b.event(conn, recorded_at=instant)
    return conn.execute(
        text("SELECT tableoid::regclass::text FROM hat_event WHERE id = :i"), {"i": event_id}
    ).scalar_one()


CHECK_DAYS = (
    date(2026, 1, 1),
    date(2026, 1, 31),
    date(2026, 10, 20),
    date(2026, 10, 31),
    date(2026, 11, 1),
    date(2026, 12, 31),
    date(2027, 1, 1),
    date(2028, 2, 29),
    date(2030, 11, 30),
)


# HAT-SCH-066 states two months ahead; the check does not read the code's constant.
SPEC_MONTHS_AHEAD = 2


def check_events__partitions_ahead(
    conn: Connection, ensure: Callable[[Connection, date], None] | None = None
) -> None:
    """After the ensure step runs at date d, partitions cover d's month and the 2 after it, and
    an event at any instant in that range lands in the partition of its UTC month."""

    def default_ensure(connection: Connection, day: date) -> None:
        ensure_event_partitions(connection, day)

    run = ensure or default_ensure
    for day in CHECK_DAYS:
        run(conn, day)
        months = months_to_cover(day, SPEC_MONTHS_AHEAD)
        children = _children(conn)
        for first in months:
            assert partition_name(first) in children, f"no partition for {first:%Y-%m} at {day}"
        for first in months:
            lower, upper = partition_bounds(first)
            instants = (lower, lower + timedelta(microseconds=1), upper - timedelta(microseconds=1))
            for instant in instants:
                assert month_of(instant) == first
                routed = _routed_to(conn, instant)
                assert routed == partition_name(first), (
                    f"an event at {instant.isoformat()} landed in {routed}, "
                    f"not {partition_name(first)}"
                )
    # The schema upkeep run (what startup does) covers the current month and two more.
    ensure_hat_schema(conn)
    moment = datetime.now(UTC)
    today = moment.date()
    children = _children(conn)
    for first in months_to_cover(today, SPEC_MONTHS_AHEAD):
        assert partition_name(first) in children, f"ensure_hat_schema left {first:%Y-%m} out"
    assert _routed_to(conn, moment) == partition_name(months_to_cover(today, 0)[0])
