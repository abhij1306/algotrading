"""PBT manage.* (HAT-EXEC-060..064, HAT-DEC-B17, HAT-RISK-070, 06 §6): broker stops and exits of
real positions (`app.hat.execution.protect`, `fills`, `manage`) on SQLite, through the real Fyers
adapter over `FakeBroker`. Nothing reaches a real broker."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from hat.fakes.broker import accepted, rejected, unconfirmed
from hat_engine_support import exit_stack
from hat_manage_support import TICK, ManageRig, at
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine import exits
from app.hat.execution.protect import (
    ALERT_GTT_MISSING,
    ALERT_GTT_SWAP_FAILED,
    ALERT_GTT_UNVERIFIED,
    ALERT_STOP_NOT_MOVED,
    ALERT_STOP_NOT_PLACED,
    STOP_DAY,
    STOP_GTT,
    broker_trigger,
)
from app.hat.models import HatEvent, HatOrder, HatPosition
from app.hat.risk import controls

D = Decimal
WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "cancel_gtt")
STOP_WRITES = ("place_order", "modify_order", "place_gtt", "modify_gtt")
UNAVAILABLE = {"s": "error", "message": "Fyers modify timeout/unavailable"}
NOT_FOUND = {"s": "error", "message": "GTT not found"}
SESSION_TIMES = st.sampled_from(
    [at(10, 40), at(12, 0), at(15, 10), at(15, 24), at(15, 26), at(15, 29), at(16, 0)]
)


def _opened(rig: ManageRig, when: datetime, qty: int | None = None) -> HatPosition:
    """An entry routed at 10:30 and filled (all of it, or `qty`) by the sync at `when`."""
    outcome = rig.enter(at(10, 30))
    row = rig.entry_row(outcome.signal_id)
    assert row is not None
    rig.fill(row, qty or row.qty, D(row.price))
    rig.sync(when)
    position = rig.position(outcome.signal_id)
    assert position is not None
    return position


def _on_tick(trigger: Decimal) -> bool:
    return trigger % TICK == 0


def _writes_since(rig: ManageRig, start: int) -> list[tuple[str, object]]:
    return [(name, args) for name, args in rig.fake.calls[start:] if name in WRITES]


# ---------------------------------------------------------------------------------------
# HAT-EXEC-060, HAT-DEC-B17: a real fill rests on a broker stop for its open quantity
# ---------------------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    chunks=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=3),
    times=st.lists(SESSION_TIMES, min_size=3, max_size=3),
    refusals=st.integers(min_value=0, max_value=2),
)
@example(chunks=[1], times=[at(10, 40)] * 3, refusals=2)
def test_real_fill_protected(chunks: list[int], times: list[datetime], refusals: int) -> None:
    """HAT-EXEC-060, HAT-DEC-B17: each entry fill the sync reads is followed, before any other
    broker write, by the position's stop: placed for the open quantity when none rests (an
    intraday SL-M in the session before 15:25, a GTT from 15:25 on and after the close), resized
    to the open quantity when one does. Its trigger is on the tick and never above the stop. A
    stop the broker refuses twice is alerted and placed by the next sync. Harm: a filled
    position sits at the broker with no stop, or with a stop for fewer shares than it holds."""
    times = sorted(times)
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        outcome = rig.enter(at(10, 30))
        row = rig.entry_row(outcome.signal_id)
        assert row is not None
        part = max(1, row.qty // sum(chunks))
        first_place = "place_order" if times[0] < at(15, 25) else "place_gtt"
        rig.fake.script(first_place, *[rejected("Insufficient margin")] * refusals)
        filled = 0
        for n, (chunk, when) in enumerate(zip(chunks, times, strict=False)):
            qty = row.qty - filled if n == len(chunks) - 1 else min(part * chunk, row.qty - filled)
            if qty <= 0:
                break
            rig.fill(row, qty, D(row.price))
            filled += qty
            start = len(rig.fake.calls)
            rig.sync(when)
            position = rig.position(outcome.signal_id)
            assert position is not None
            assert position.qty_open == filled
            writes = _writes_since(rig, start)
            stops = rig.stops(position.id)
            if refusals == 2 and n == 0:
                assert [name for name, _ in writes] == [first_place, first_place]
                assert stops == []
                assert ALERT_STOP_NOT_PLACED in rig.codes()
                rig.sync(when)  # nothing new: the sync places the stop again
                stops = rig.stops(position.id)
            [stop] = stops
            assert stop.qty == position.qty_open
            assert stop.trigger_price <= position.current_stop
            assert _on_tick(D(stop.trigger_price))
            if n == 0:  # the first stop: intraday before 15:25, a GTT from then on
                assert (stop.purpose == STOP_DAY) == (when < at(15, 25))
            # the sync's only broker writes are the stop's (a sell stop for this position)
            assert all(name in STOP_WRITES for name, _ in writes)
            assert all(args["side"] == "SELL" for name, args in writes if name == "place_order")


# ---------------------------------------------------------------------------------------
# HAT-EXEC-064, HAT-RISK-070: the broker stop follows the stop up, never down
# ---------------------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    night=st.booleans(),
    moves=st.lists(
        st.tuples(st.decimals(min_value=-3, max_value=4, places=2), st.booleans()),
        min_size=1,
        max_size=5,
    ),
)
def test_broker_stop_only_tightens(night: bool, moves: list[tuple[Decimal, bool]]) -> None:
    """HAT-EXEC-064, HAT-RISK-070, HAT-SCH-081: when the position's stop rises, its working
    broker stop (intraday or GTT) is modified up to it, rounded down to the tick, so it is never
    above the stop; it is never modified down; a modify the broker refuses leaves the stop where
    it was and is alerted. Harm: a loosened broker stop, or a broker stop above the price HAT
    holds, sells the position on noise."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0) if night else at(10, 40))
        [stop] = rig.stops(position.id)
        sent: list[Decimal] = [D(stop.trigger_price)]
        for delta, refused in moves:
            wanted = D(position.current_stop) + delta
            if wanted > D(position.current_stop):  # the database refuses a lower stop
                position.current_stop = wanted
                session.commit()
            modify = "modify_gtt" if night else "modify_order"
            before = D(stop.trigger_price)
            refused = refused and broker_trigger(D(position.current_stop), TICK) > before
            if refused:  # the one modify this follow sends is refused
                rig.fake.script(modify, UNAVAILABLE)
            rig.desk.follow(session, position.id, at(16, 5) if night else at(11, 0))
            session.refresh(position)
            [stop] = rig.stops(position.id)
            now = D(stop.trigger_price)
            assert now >= before
            assert now <= D(position.current_stop)
            assert _on_tick(now)
            if refused:
                assert now == before
                assert ALERT_STOP_NOT_MOVED in rig.codes()
            else:
                assert now == broker_trigger(D(position.current_stop), TICK)
        modify = "modify_gtt" if night else "modify_order"
        sent += [D(str(args[1]["trigger_price"])) for args in rig.fake.calls_named(modify)]
        assert sent == sorted(sent)


# ---------------------------------------------------------------------------------------
# HAT-EXEC-061: the 15:25 swap places the GTT before it cancels the intraday stop
# ---------------------------------------------------------------------------------------

GTT_REPLIES = st.sampled_from(
    ["accepted", "rejected", "unconfirmed", "unconfirmed_landed", "raises"]
)


def _gtt_reply(kind: str) -> object:
    return {
        "accepted": accepted("G-9"),
        "rejected": rejected("GTT limit reached"),
        "unconfirmed": unconfirmed(),
        "unconfirmed_landed": unconfirmed(landed=True),
        "raises": RuntimeError("socket closed"),
    }[kind]


@settings(max_examples=30, deadline=None)
@given(first=GTT_REPLIES, second=GTT_REPLIES)
def test_swap_leaves_no_gap(first: str, second: str) -> None:
    """HAT-EXEC-061: `gtt.swap` cancels the intraday stop only after the broker confirmed the
    GTT that replaces it; a GTT the broker refuses (after the one retry) or does not confirm
    leaves the intraday stop working, raises `gtt.swap_failed` and fails the job so it is tried
    again. Harm: the day stop is cancelled first and the position spends the night with no stop
    if the GTT fails."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(10, 40))
        rig.fake.script("place_gtt", _gtt_reply(first), _gtt_reply(second))
        start = len(rig.fake.calls)
        report = rig.manager.swap_stops(session, rig.account_id, at(15, 25))
        names = [name for name, _ in _writes_since(rig, start)]
        working = {row.purpose for row in rig.stops(position.id)}
        if "cancel_order" in names:
            assert names.index("place_gtt") < names.index("cancel_order")
        confirmed = first == "accepted" or (first == "rejected" and second == "accepted")
        if confirmed:
            assert working == {STOP_GTT}
            assert report.failed == []
        else:
            assert STOP_DAY in working
            assert "cancel_order" not in names
            assert ALERT_GTT_SWAP_FAILED in rig.codes()
            assert report.failed == [position.id]


# ---------------------------------------------------------------------------------------
# HAT-EXEC-063, HAT-OPS-024: the nightly verify covers every open real position
# ---------------------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    reply=st.sampled_from(["ok", "not_found", "unavailable"]),
    raised=st.decimals(min_value=0, max_value=3, places=2),
)
def test_verify_covers(reply: str, raised: Decimal) -> None:
    """HAT-EXEC-063, HAT-OPS-024: each night every open real position's GTT is refreshed to the
    current stop (`gtt.refreshed`). A GTT the broker does not confirm is alerted
    (`gtt.missing`), ended in HAT's book and placed again; when the broker cannot be reached
    the position is alerted (`gtt.unverified`) and nothing is placed, since the old GTT may
    still rest there. Harm: a GTT that triggered, expired or was never placed goes unnoticed and
    the position opens the next day without a stop."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        if raised > 0:
            position.current_stop = D(position.current_stop) + raised
            session.commit()
        if reply != "ok":
            rig.fake.script("modify_gtt", UNAVAILABLE if reply == "unavailable" else NOT_FOUND)
        places = rig.fake.count("place_gtt")
        rig.manager.verify_stops(session, rig.account_id, at(23, 0))
        goal = broker_trigger(D(position.current_stop), TICK)
        stops = rig.stops(position.id)
        if reply == "unavailable":
            assert ALERT_GTT_UNVERIFIED in rig.codes()
            assert rig.fake.count("place_gtt") == places
            assert [row.id for row in stops] == [old.id]
            return
        [gtt] = stops
        assert gtt.purpose == STOP_GTT
        assert D(gtt.trigger_price) == goal
        if reply == "ok":
            assert gtt.id == old.id
            assert _event_count(session, gtt, "gtt.refreshed") == 1
        else:
            assert ALERT_GTT_MISSING in rig.codes()
            assert gtt.id != old.id
            assert rig.fake.count("place_gtt") == places + 1
            session.refresh(old)
            assert old.status == "expired"


def _event_count(session: Session, row: HatOrder, kind: str) -> int:
    return len(
        session.scalars(
            select(HatEvent.id).where(HatEvent.aggregate_id == row.id, HatEvent.event_type == kind)
        ).all()
    )


# ---------------------------------------------------------------------------------------
# 06 §6, HAT-EXEC-070: real exits run the shared stack, and no kill switch blocks them
# ---------------------------------------------------------------------------------------

KILLS = st.sampled_from(["none", "account", "bot", "pause"])


@settings(max_examples=25, deadline=None)
@given(
    kill=KILLS,
    path=st.lists(st.decimals(min_value=90, max_value=140, places=1), min_size=1, max_size=6),
)
def test_real_exits_never_blocked(kill: str, path: list[Decimal]) -> None:
    """06 §6, HAT-EXEC-070, HAT-DEC-B17: a real position's live exits come from the same exit
    stack as a shadow position's, and are sent as MARKET SELL CNC orders whatever kill switch or
    pause is on: the broker sees the same sales with and without one. The hard stop is left to
    the broker stop resting under the position. Harm: a tripped kill switch traps a position
    that its exits should sell, or HAT sells twice at the stop."""
    sales: dict[str, list[tuple[str, int]]] = {}
    for variant in ("none", kill):
        with Session(memory_engine()) as session:
            rig = ManageRig(session, exits=exit_stack("partials"))
            position = _opened(rig, at(10, 40))
            if variant == "pause":
                assert controls.pause_bot(session, rig.world.bot_id, at(10, 45), chat_id=7)
            elif variant != "none":
                bot = rig.world.bot_id if variant == "bot" else None
                controls.trip_kill(
                    session, scope=variant, bot_id=bot, reason="manual", now=at(10, 45)
                )
            session.commit()
            start = len(rig.fake.calls)
            for n, price in enumerate(path):
                session.refresh(position)
                at_stop = price <= D(position.current_stop)
                before = rig.fake.count("place_order")
                view = exits.ExitView(price=price, days_held=1)
                rig.manager.on_price(session, position.id, view, at(11, n))
                if at_stop:  # the broker stop sells; HAT sends nothing at the hard stop
                    assert rig.fake.count("place_order") == before
            orders = [args for name, args in rig.fake.calls[start:] if name == "place_order"]
            assert all(o["side"] == "SELL" and o["product"] == "CNC" for o in orders)
            assert all(o["type"] == "MARKET" for o in orders)
            sales[variant] = [(o["type"], o["quantity"]) for o in orders]
    assert sales[kill] == sales["none"]
