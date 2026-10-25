"""Database rules of the runtime tables and the event log, each as a check
body over a Postgres connection. Same contract as `hat_db_rules.py`: a check raises
AssertionError when the database does not enforce its rule."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import hat_db_builders as b
from hat_db_rules import accepts, rejects
from sqlalchemy import Connection, Engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.hat import models as m
from app.hat.governance import Mode
from app.hat.ids import uuid7
from app.hat.notify import RecordingNotifier
from app.hat.ops.jobs import JobError
from app.hat.ops.partitions import (
    CODE_PARTITIONS_SHORT,
    ensure_event_partitions,
    events_partitions,
    existing_partitions,
    month_of,
    months_to_cover,
    partition_bounds,
    partition_name,
)
from app.hat.schema import ensure_hat_schema

if TYPE_CHECKING:
    from app.hat.engine.entries import EntryRequest

# --- runtime tables (.. ) -------------------------------------------


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
        " risk_amount, mode, state, risk_decision, confidence, confidence_basis)"
        " VALUES (:id, :bot, :version, :policy, 'race', :instrument, :day, now(), 100, 95, 1, 5,"
        " 'shadow', 'Triggered', '{}', 'low', '{}')"
    )
    outcomes: list[str] = []
    first_in = threading.Event()
    release = threading.Event()
    pids: dict[bool, int] = {}  # each worker's backend, by `hold`

    def worker(hold: bool) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            pids[hold] = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
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
        # the racer itself is blocked by the holder, not any other session's lock wait
        blocked = text("SELECT CAST(:holder AS int) = ANY(pg_blocking_pids(:racer))")
        while True:
            if len(pids) == 2:
                with engine.connect() as probe:
                    if probe.execute(
                        blocked, {"holder": pids[True], "racer": pids[False]}
                    ).scalar():
                        break
            assert time.monotonic() < deadline, "the racer never waited on the holder's lock"
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

    for mode in Mode:
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


# --- invariant 1 --------------------------------------------------------------

STOPS = (
    Decimal("0.0001"),
    Decimal("94.9999"),
    Decimal("95"),
    Decimal("95.0001"),
    Decimal("100"),
    Decimal("1234.5678"),
    Decimal("99999999999999.9999"),
)


def check_governance__signal_confidence_immutable(conn: Connection) -> None:
    """D132, a signal's confidence label and basis never change after insert, and
    the label is one of high, medium, low."""
    w = b.world(conn)
    table = m.HatSignal.__table__
    labels = ("high", "medium", "low")
    for n, old in enumerate(labels):
        signal = b.signal(
            conn,
            w,
            playbook=f"c{n}",
            confidence=old,
            confidence_basis={"reason": "r", "trades": 40},
        )

        def change(s: uuid.UUID = signal, **values: object) -> None:
            conn.execute(update(table).where(table.c.id == s).values(**values))

        for new in labels:
            if new != old:
                rejects(conn, f"relabelling {old} as {new}", lambda v=new: change(confidence=v))
        rejects(
            conn,
            "rewriting the basis",
            lambda: change(confidence_basis={"reason": "r", "trades": 41}),
        )
        rejects(
            conn,
            "relabelling with a state change",
            lambda: change(state="Rejected", confidence_basis={}),
        )
        accepts(conn, "a state change", lambda: change(state="Rejected"))
        accepts(conn, "the same label and basis written again", lambda o=old: change(confidence=o))
        kept = conn.execute(
            select(table.c.confidence, table.c.confidence_basis).where(table.c.id == signal)
        ).one()
        assert tuple(kept) == (old, {"reason": "r", "trades": 40})
    for n, bad in enumerate(("HIGH", "", "none", "unknown")):
        rejects(
            conn,
            f"the label {bad!r}",
            lambda v=bad, i=n: b.signal(conn, w, playbook=f"bad{i}", confidence=v),
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
            .values(current_stop=Decimal("99"), state="PartiallyExited")
        ),
    )
    accepts(
        conn,
        "an update that does not touch the stop",
        lambda: conn.execute(
            update(table).where(table.c.id == position).values(state="PartiallyExited", qty_open=5)
        ),
    )
    rejects(
        conn,
        "lowering every stop with one statement",
        lambda: conn.execute(update(table).values(current_stop=Decimal("0.0001"))),
    )
    # A DELETE followed by an INSERT would restart the stop below its old value (invariant 1).
    doomed = b.position(conn, w, b.signal(conn, w, playbook="doomed"))
    rejects(
        conn,
        "deleting a position",
        lambda: conn.execute(delete(table).where(table.c.id == doomed)),
    )
    kept = conn.execute(select(table.c.id).where(table.c.id == doomed))
    assert kept.scalar_one_or_none() == doomed, "a refused delete removed the position"
    rejects(conn, "deleting every position", lambda: conn.execute(delete(table)))


# --- the rescale exception to invariant 1 --------------------------

RESCALE_FROM = {"qty_open": 10, "avg_entry": "100", "initial_stop": "95", "current_stop": "98"}
RESCALE_TO = {"qty_open": 20, "avg_entry": "50", "initial_stop": "47.5", "current_stop": "49"}


def _rescaled_event(
    conn: Connection, position_id: uuid.UUID, frm: dict, to: dict, event_type: str = ""
) -> None:
    b.event(
        conn,
        aggregate_type="position",
        aggregate_id=position_id,
        event_type=event_type or "position.rescaled",
        payload={"action": "2024-01-10:split", "factor": "0.5", "from": frm, "to": to},
    )


def _as_row(values: dict) -> dict:
    return {k: Decimal(v) if isinstance(v, str) else v for k, v in values.items()}


def _nudged(value: int | str) -> int | str:
    return value + 1 if isinstance(value, int) else str(Decimal(value) + Decimal("0.0001"))


def check_governance__rescale_moves_stop(conn: Connection) -> None:
    """A lowering UPDATE goes through only with a `position.rescaled` event of that position that
    names exactly its old and new quantity, entry and stops."""
    w = b.world(conn)
    table = m.HatPosition.__table__

    def make(tag: str) -> uuid.UUID:
        return b.position(
            conn, w, b.signal(conn, w, playbook=f"rescale-{tag}"), **_as_row(RESCALE_FROM)
        )

    def move(p: uuid.UUID, values: dict = RESCALE_TO) -> None:
        conn.execute(update(table).where(table.c.id == p).values(**_as_row(values)))

    exact = make("exact")
    _rescaled_event(conn, exact, RESCALE_FROM, RESCALE_TO)
    accepts(conn, "a rescale with its event", lambda: move(exact))
    row = conn.execute(select(table).where(table.c.id == exact)).mappings().one()
    assert {k: row[k] for k in RESCALE_TO} == _as_row(RESCALE_TO), "the rescale was not kept"
    lower = {**RESCALE_TO, "current_stop": "48"}
    rejects(conn, "lowering again after the rescale", lambda: move(exact, lower))
    bare = make("bare")
    rejects(conn, "a rescale with no event", lambda: move(bare))
    mine, other = make("mine"), make("other")
    _rescaled_event(conn, other, RESCALE_FROM, RESCALE_TO)
    rejects(conn, "a rescale with another position's event", lambda: move(mine))
    typed = make("typed")
    _rescaled_event(conn, typed, RESCALE_FROM, RESCALE_TO, event_type="stop.moved")
    rejects(conn, "a rescale whose event is another type", lambda: move(typed))
    for side in ("from", "to"):
        for key in RESCALE_FROM:
            p = make(f"{side}-{key}")
            frm, to = dict(RESCALE_FROM), dict(RESCALE_TO)
            (frm if side == "from" else to)[key] = _nudged((frm if side == "from" else to)[key])
            _rescaled_event(conn, p, frm, to)
            rejects(conn, f"a rescale whose event differs in {side}.{key}", lambda p=p: move(p))


SIGNAL_STATES = (
    "Watchlisted", "Triggered", "AwaitingApproval", "EntryPlaced", "Open", "PartiallyExited",
    "ExitPending", "Closed", "Rejected", "Expired", "Cancelled",
)  # fmt: skip
POSITION_STATES = ("Open", "PartiallyExited", "ExitPending", "Closed")
BAD_STATES = ("open", "triggered", "OPEN", "trailing", "Stopped", "", " Open")


def check_runtime__state_domain(conn: Connection) -> None:
    """`hat_signal.state` holds one of the 11 lifecycle states and `hat_position.state`
    one of the four a position can be in; any other word is refused."""
    w = b.world(conn)
    for n, state in enumerate(SIGNAL_STATES):
        accepts(
            conn,
            f"signal state {state}",
            lambda state=state, n=n: b.signal(conn, w, playbook=f"s{n}", state=state),
        )
    for n, state in enumerate(BAD_STATES):
        rejects(
            conn,
            f"signal state {state!r}",
            lambda state=state, n=n: b.signal(conn, w, playbook=f"bad{n}", state=state),
        )
    for n, state in enumerate(POSITION_STATES):
        signal = b.signal(conn, w, playbook=f"p{n}")
        accepts(
            conn,
            f"position state {state}",
            lambda state=state, signal=signal: b.position(conn, w, signal, state=state),
        )
    for n, state in enumerate((*BAD_STATES, "Triggered", "Watchlisted", "Rejected")):
        signal = b.signal(conn, w, playbook=f"pb{n}")
        rejects(
            conn,
            f"position state {state!r}",
            lambda state=state, signal=signal: b.position(conn, w, signal, state=state),
        )


def _committed_bot_world(
    engine: Engine, config: dict[str, object] | None = None
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID], uuid.UUID]:
    """A shadow bot with a live version and a universe of two instruments, committed for real:
    (bot, user, instruments, risk policy version)."""
    with engine.begin() as connection:
        market = b.market(connection)
        strategy = b.strategy(connection, market)
        version = b.strategy_version(connection, strategy, **({"config": config} if config else {}))
        account = b.broker_account(connection)
        instruments = [
            b.instrument(connection, market, isin=f"INE{n:09d}", symbol=f"PB{n}") for n in (1, 2)
        ]
        universe = b.universe(connection, market)
        uversion = b.universe_version(connection, universe)
        for instrument in instruments:
            b.add(
                connection,
                m.HatUniverseMember,
                universe_id=universe,
                instrument_id=instrument,
                effective_from=b.TRADE_DAY - timedelta(days=30),
                reason="constituent",
            )
        policy = b.risk_policy(connection, version=int(uuid.uuid4().int % 10**9) + 100)
        bot = b.bot(
            connection,
            strategy,
            account,
            execution_mode="shadow",
            live_version_id=version,
            universe_version_id=uversion,
        )
    return bot, b.USER_ID, instruments, policy


def _second_bot(engine: Engine, bot: uuid.UUID) -> uuid.UUID:
    """Another shadow bot of `bot`'s owner, strategy version and universe, on another broker
    account of the owner."""
    with engine.begin() as connection:
        row = connection.execute(select(m.HatBot).where(m.HatBot.id == bot)).one()
        return b.bot(
            connection,
            row.strategy_id,
            b.broker_account(connection),
            execution_mode="shadow",
            live_version_id=row.live_version_id,
            universe_version_id=row.universe_version_id,
            risk_cluster="c2",
        )


def _race_entries(
    engine: Engine,
    holder: "EntryRequest",
    racer: "EntryRequest",
) -> list[bool]:
    """Run two `open_entry` transactions at once, the holder pausing between its risk decision
    and its insert until the racer waits on an advisory lock (or 5 s pass); whether each took a
    position, in the order they committed. The caller empties the runtime tables."""
    from sqlalchemy.orm import Session

    from app.hat.engine import entries
    from app.hat.risk.decision import SignalContext
    from app.hat.risk.service import evaluate_signal

    first_in = threading.Event()
    release = threading.Event()
    taken: list[bool] = []

    def hold_between_decision_and_insert(ctx: SignalContext):  # noqa: ANN202
        decision = evaluate_signal(ctx)
        first_in.set()
        release.wait(10)
        return decision

    def worker(request: EntryRequest, hold: bool) -> None:
        if not hold:
            first_in.wait(5)
        with Session(engine) as session:
            decide = hold_between_decision_and_insert if hold else evaluate_signal
            outcome = entries.open_entry(session, request, decide=decide)
            session.commit()
            taken.append(outcome.position_id is not None)

    holding = threading.Thread(target=worker, args=(holder, True))
    racing = threading.Thread(target=worker, args=(racer, False))
    try:
        holding.start()
        racing.start()
        deadline = time.monotonic() + 5
        waiting = text(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            " AND wait_event = 'advisory' AND datname = current_database()"
        )
        while time.monotonic() < deadline and racing.is_alive():
            with engine.connect() as probe:
                if probe.execute(waiting).scalar_one() >= 1:
                    break
            time.sleep(0.05)  # noqa: ASYNC251
        release.set()
        holding.join(10)
        racing.join(10)
        return taken
    finally:
        release.set()


def _held(engine: Engine, bots: list[uuid.UUID]) -> int:
    with engine.connect() as probe:
        return probe.execute(
            select(func.count()).select_from(m.HatPosition).where(m.HatPosition.bot_id.in_(bots))
        ).scalar_one()


def truncate_bypassing_guards(connection: Connection) -> None:
    """Let a test's cleanup TRUNCATE `hat_event` (C10's guard refuses it): triggers do not fire
    for this transaction (the test database's superuser)."""
    connection.execute(text("SET LOCAL session_replication_role = replica"))


def _empty_runtime(engine: Engine) -> None:
    with engine.begin() as connection:
        truncate_bypassing_guards(connection)
        connection.execute(
            text(
                "TRUNCATE hat_event, hat_fill, hat_order, hat_position, hat_signal, hat_bot CASCADE"
            )
        )


def check_runtime__per_bot_serialised(conn: Connection) -> None:
    """Two entries of one bot evaluated at the same time cannot both take the last
    slot. The first holds the bot's lock between its risk decision and its insert; the second
    must wait for it, then see the first one's position and be rejected for the cap."""
    from hat_risk_support import AT, TRADE_DAY, policy_with, valid_ctx

    from app.hat.engine import entries
    from app.hat.risk.policy import PolicyVersion

    engine = conn.engine
    bot, _, instruments, policy_id = _committed_bot_world(engine)
    version = PolicyVersion(
        policy_id, 1, datetime(2026, 1, 1, tzinfo=UTC), None, policy_with(max_open_positions=1)
    )

    def request(instrument: uuid.UUID) -> entries.EntryRequest:
        return entries.EntryRequest(
            bot_id=bot,
            playbook="pb",
            instrument_id=instrument,
            trade_date=TRADE_DAY,
            triggered_at=AT,
            context=valid_ctx(policy_versions=[version]),
        )

    try:
        taken = _race_entries(engine, request(instruments[0]), request(instruments[1]))
        held = _held(engine, [bot])
        assert sorted(taken) == [False, True], taken
        assert held == 1, f"{held} positions for a bot whose cap is 1"
    finally:
        _empty_runtime(engine)


def check_runtime__owner_serialised(conn: Connection) -> None:
    """Two bots of one owner, on different broker accounts, entering at the same
    time cannot both fit under the global open-risk cap when it has room for one entry. The
    first holds the owner's lock between its risk decision and its insert; the second (another
    bot, so the bot lock does not serialise them) must wait for it, then see the first one's
    position and be rejected for `global_open_risk`."""
    from hat_risk_support import AT, TRADE_DAY, policy_with, valid_ctx

    from app.hat.engine import entries
    from app.hat.risk.policy import PolicyVersion
    from app.hat.risk.service import evaluate_signal

    engine = conn.engine
    bot, _, instruments, policy_id = _committed_bot_world(engine)
    other = _second_bot(engine, bot)
    version = PolicyVersion(policy_id, 1, datetime(2026, 1, 1, tzinfo=UTC), None, policy_with())
    risk = evaluate_signal(valid_ctx(total_equity=Decimal(10**12))).risk_amount
    total_equity = (risk + 1) / Decimal("0.015")  # room for one entry of either bot

    def request(bot_id: uuid.UUID, instrument: uuid.UUID) -> entries.EntryRequest:
        return entries.EntryRequest(
            bot_id=bot_id,
            playbook="pb",
            instrument_id=instrument,
            trade_date=TRADE_DAY,
            triggered_at=AT,
            context=valid_ctx(policy_versions=[version], total_equity=total_equity),
        )

    try:
        taken = _race_entries(engine, request(bot, instruments[0]), request(other, instruments[1]))
        held = _held(engine, [bot, other])
        assert sorted(taken) == [False, True], taken
        assert held == 1, f"{held} positions for an owner whose open-risk cap fits 1"
    finally:
        _empty_runtime(engine)


def check_runtime__publish_serialised(conn: Connection) -> None:
    """Two runs of one bot's nightly publish for one day at the same
    time write one `watchlist.published`. The first holds the bot's lock inside the scan, after
    its "already published?" read; the second must wait, then see the first one's event."""
    import polars as pl
    import yaml
    from hat_risk_support import AT, TRADE_DAY
    from sqlalchemy.orm import Session

    from app.hat.engine import watchlist
    from app.hat.engine.scan import ScanInputs
    from app.hat.engine.strategy.versions import seed_text

    engine = conn.engine
    config = yaml.safe_load(seed_text("stage2-contraction-breakout"))
    bot, _, _, _ = _committed_bot_world(engine, config)
    inputs = ScanInputs(as_of=TRADE_DAY, members=(), bars=pl.DataFrame(), regime_closes=())
    first_in = threading.Event()
    release = threading.Event()
    real_scan = watchlist.scan_bot
    results: list[str | None] = []

    def held_scan(*args: object):  # noqa: ANN202
        if threading.current_thread().name == "holder":
            first_in.set()
            release.wait(10)
        return real_scan(*args)

    def worker() -> None:
        if threading.current_thread().name != "holder":
            first_in.wait(5)
        with Session(engine) as session:
            published = watchlist.scan_and_publish(session, bot, inputs, TRADE_DAY, AT)
            session.commit()
            results.append(published.skipped)

    holder = threading.Thread(target=worker, name="holder")
    racer = threading.Thread(target=worker, name="racer")
    watchlist.scan_bot = held_scan
    try:
        holder.start()
        racer.start()
        deadline = time.monotonic() + 5
        waiting = text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
        while time.monotonic() < deadline and racer.is_alive():
            with engine.connect() as probe:
                if probe.execute(waiting).scalar_one() >= 1:
                    break
            time.sleep(0.05)  # noqa: ASYNC251
        release.set()
        holder.join(10)
        racer.join(10)
        with engine.connect() as probe:
            events = probe.execute(
                text(
                    "SELECT count(*) FROM hat_event"
                    " WHERE bot_id = :bot AND event_type = 'watchlist.published'"
                ),
                {"bot": bot},
            ).scalar_one()
        assert events == 1, f"{events} publishes of one bot's watchlist for one day"
        assert sorted(results, key=str) == [None, "already_published"], results
    finally:
        watchlist.scan_bot = real_scan
        release.set()
        with engine.begin() as connection:
            truncate_bypassing_guards(connection)
            connection.execute(text("TRUNCATE hat_event, hat_watchlist_entry, hat_bot CASCADE"))


# --- event log (061, 066, 068) -------------------------------------------------

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
        conn, "truncating the event log", lambda: conn.execute(text("TRUNCATE hat_event"))
    )
    for partition in sorted(existing_partitions(conn)):
        rejects_append_only(
            conn,
            f"truncating the partition {partition}",
            lambda p=partition: conn.execute(text(f"TRUNCATE {p}")),
        )
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


# Far past every other check's months: the job commits its partitions (dropped with hat_event).
JOB_DAY = date(2091, 3, 15)


def check_events__partitions_job(engine: Engine) -> None:
    """The `events.partitions` job: an event two months past a day with no partitions is refused
    before the job runs for that day and lands in its month's partition after; a run that leaves
    less than one month ahead alerts `events.partitions_short` and fails with that code."""
    later = datetime(2091, 5, 2, 12, tzinfo=UTC)
    with engine.connect() as conn, conn.begin():
        rejects(
            conn, "an event in a month with no partition", lambda: b.event(conn, recorded_at=later)
        )
    notifier = RecordingNotifier()
    result = events_partitions(engine, JOB_DAY, notifier)
    assert result.rows == 3
    assert notifier.alerts == []
    with engine.connect() as conn, conn.begin():
        routed = _routed_to(conn, later)
        conn.rollback()
    assert routed == partition_name(date(2091, 5, 1))
    short_day = date(2091, 9, 10)
    try:
        events_partitions(engine, short_day, notifier, ahead=0)
    except JobError as error:
        code = error.code
    else:
        code = None
    assert code == CODE_PARTITIONS_SHORT
    assert [a[:2] for a in notifier.alerts] == [(CODE_PARTITIONS_SHORT, short_day)]


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


# states two months ahead; the check does not read the code's constant.
SPEC_MONTHS_AHEAD = 2


def check_events__partitions_ahead(conn: Connection) -> None:
    """After the ensure step runs at date d, partitions cover d's month and the 2 after it, and
    an event at any instant in that range lands in the partition of its UTC month."""
    for day in CHECK_DAYS:
        ensure_event_partitions(conn, day)
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
