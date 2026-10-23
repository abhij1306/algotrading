"""PBT manage.* (HAT-EXEC-060..064, HAT-DEC-B17, HAT-RISK-070, 06 §6): broker stops and exits of
real positions (`app.hat.execution.protect`, `fills`, `manage`) on SQLite, through the real Fyers
adapter over `FakeBroker`. Nothing reaches a real broker."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hat.fakes.broker import accepted, rejected, unconfirmed
from hat_engine_support import TRADE_DAY, exit_stack
from hat_exec_support import SYMBOLS
from hat_manage_support import TICK, TICKER, ManageRig, at
from hat_risk_support import CALENDAR
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.daily.live import ExitViews
from app.hat.data.bars import Bar
from app.hat.data.corporate_actions import TYPE_DIVIDEND, TYPE_NOTICE, TYPE_SPLIT
from app.hat.data.instruments import fyers_instruments
from app.hat.engine import exits, journal, lifecycle
from app.hat.execution.broker import BrokerUnavailableError, InvalidIntentError
from app.hat.execution.entry_timeout import EntryWatch
from app.hat.execution.fills import ALERT_FILL_EXCESS, NOT_MARKETED, RESTING
from app.hat.execution.manage import ALERT_EXIT_NOT_PLACED
from app.hat.execution.protect import (
    ALERT_GTT_MISSING,
    ALERT_GTT_SWAP_FAILED,
    ALERT_GTT_UNFILLED,
    ALERT_GTT_UNVERIFIED,
    ALERT_STOP_NOT_MOVED,
    ALERT_STOP_NOT_PLACED,
    CODE_SERIES_UNMAPPED,
    EVENT_GTT_TRIGGERED,
    LIVE,
    STOP_DAY,
    STOP_GTT,
    broker_trigger,
)
from app.hat.execution.series import ALERT_SERIES_CHANGED
from app.hat.execution.tags import encode_tag
from app.hat.models import (
    HatBrokerSymbol,
    HatCorporateAction,
    HatEvent,
    HatInstrument,
    HatOrder,
    HatPosition,
    HatSignal,
)
from app.hat.risk import controls
from app.hat.telegram.render import URGENT_ALERTS
from app.hat.time import IST, trade_date_of

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
    resize_refused=st.booleans(),
)
@example(chunks=[1], times=[at(10, 40)] * 3, refusals=2, resize_refused=False)
@example(chunks=[1, 1], times=[at(10, 40)] * 3, refusals=0, resize_refused=True)
def test_real_fill_protected(
    chunks: list[int], times: list[datetime], refusals: int, resize_refused: bool
) -> None:
    """HAT-EXEC-060, HAT-DEC-B17: each entry fill the sync reads is followed, before any other
    broker write, by the position's stop: placed for the open quantity when none rests (an
    intraday SL-M in the session before 15:25, a GTT from 15:25 on and after the close), resized
    to the open quantity when one does. Its trigger is on the tick and never above the stop. A
    stop the broker refuses twice is alerted and placed by the next sync; a resize it refuses is
    alerted and sent again by the next sync. Harm: a filled
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
            resting = rig.stops(rig.position(outcome.signal_id).id) if n else []  # type: ignore[union-attr]
            refuse = resize_refused and len(resting) == 1
            if refuse:
                modify = "modify_order" if resting[0].purpose == STOP_DAY else "modify_gtt"
                rig.fake.script(modify, {"s": "error", "message": "Fyers modify refused"})
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
            if refuse:
                [short] = stops
                assert short.qty < position.qty_open
                assert ALERT_STOP_NOT_MOVED in rig.codes()
                rig.sync(when)  # nothing new: the sync resizes the stop again
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
# HAT-EXEC-060, HAT-DEC-B17 (F2): the rest of a part-filled entry is cancelled once the
# position is sold, and any share of it that still fills is held and protected
# ---------------------------------------------------------------------------------------

WORKING_AT_BROKER = 6  # Fyers orderbook status of a pending order
EXPIRED_AT_BROKER = 7
STEPS = st.one_of(
    st.tuples(st.just("entry"), st.integers(min_value=1, max_value=3)),
    st.just(("stop", 0)),
    st.just(("flatten", 0)),
)


def _broker_holds(rig: ManageRig) -> int:
    """The shares the broker holds for the account: its trade book, buys less sales."""
    return sum(row["side"] * row["tradedQty"] for row in rig.fake.tradebook)  # type: ignore[union-attr]


def _book_row(rig: ManageRig, row: HatOrder) -> dict[str, Any]:
    [found] = [book for book in rig.fake.book if book["orderTag"] == row.tag]
    return found


def _sell_all(rig: ManageRig, step: str, when: datetime, position: HatPosition) -> None:
    """The broker sells the position: its resting day stop triggers, or HAT flattens it."""
    if step == "stop":
        for stop in rig.stops(position.id, *LIVE):
            if stop.broker_order_id:
                rig.fill(stop, stop.qty, D(stop.trigger_price))
        return
    rig.manager.flatten(rig.session, rig.account_id, when)
    for sale in rig.session.scalars(
        select(HatOrder).where(
            HatOrder.position_id == position.id,
            HatOrder.purpose == "flatten",
            HatOrder.status.in_(LIVE),
        )
    ):
        rig.fill(sale, sale.qty, D(100))


def _held_is_protected(rig: ManageRig, signal_id: object, entry: HatOrder) -> None:
    position = rig.position(signal_id)  # type: ignore[arg-type]
    assert position is not None
    held = _broker_holds(rig)
    if position.closed_at is not None:
        assert held == 0, "closed while the broker holds shares"
        assert _book_row(rig, entry)["status"] != WORKING_AT_BROKER
        return
    assert position.qty_open == held
    assert sum(stop.qty for stop in rig.stops(position.id, *LIVE)) == held


@settings(max_examples=40, deadline=None)
@given(
    first=st.integers(min_value=1, max_value=3),
    steps=st.lists(STEPS, min_size=1, max_size=6),
    cancel_lands=st.booleans(),
)
@example(first=1, steps=[("stop", 0), ("entry", 3)], cancel_lands=False)
@example(first=1, steps=[("flatten", 0), ("entry", 2)], cancel_lands=True)
def test_entry_remainder_tracked(
    first: int, steps: list[tuple[str, int]], cancel_lands: bool
) -> None:
    """HAT-EXEC-060, HAT-DEC-B17 (F2): an entry fills in chunks while its stop fills and HAT
    flattens. Once the position is sold, the unfilled rest of the entry is cancelled at the
    broker; a chunk that fills first (the cancel refused or too late) adds to the position, which
    stays open until the broker shows the entry ended, and gets a stop for it. After every sync
    HAT's open quantity equals the shares the broker holds, its live stops cover exactly those,
    and a closed position leaves no share and no working entry at the broker. Harm: shares the
    broker bought after HAT closed the position sit untracked and unprotected."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        outcome = rig.enter(at(10, 30))
        entry = rig.entry_row(outcome.signal_id)
        assert entry is not None
        assert entry.broker_order_id
        refused: list[str] = []
        cancel = rig.fake.cancel_order

        def cancel_order(order_id: str) -> Any:  # noqa: ANN401
            if order_id == entry.broker_order_id and not cancel_lands:
                refused.append(order_id)  # the remainder filled or was in transit
                return {"s": "error", "message": "Order already traded"}
            return cancel(order_id)

        rig.fake.cancel_order = cancel_order  # type: ignore[method-assign]
        unit = max(1, entry.qty // 10)
        filled = 0
        when = at(10, 40)
        for n, (step, chunk) in enumerate([("entry", first), *steps]):
            when = at(10, 40) + timedelta(minutes=5 * n)
            position = rig.position(outcome.signal_id)
            if step == "entry":
                qty = min(unit * chunk, entry.qty - filled)
                if qty <= 0 or _book_row(rig, entry)["status"] != WORKING_AT_BROKER:
                    continue
                rig.fill(entry, qty, D(entry.price))
                filled += qty
            elif position is None or position.closed_at is not None or position.qty_open == 0:
                continue
            else:
                _sell_all(rig, step, when, position)
            rig.sync(when)
            _held_is_protected(rig, outcome.signal_id, entry)
            position = rig.position(outcome.signal_id)
            assert position is not None
            if position.qty_open == 0 and position.closed_at is None:
                # sold while the entry worked: its rest was asked to be cancelled
                entry_id = entry.broker_order_id
                assert entry_id in refused or entry_id in rig.fake.calls_named("cancel_order")
        row = _book_row(rig, entry)
        if row["status"] == WORKING_AT_BROKER:
            row["status"] = EXPIRED_AT_BROKER  # a DAY limit's rest expires at the close
        rig.sync(when)
        _held_is_protected(rig, outcome.signal_id, entry)
        position = rig.position(outcome.signal_id)
        assert position is not None
        assert (position.closed_at is not None) == (position.qty_open == 0)


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
@given(first=GTT_REPLIES, second=GTT_REPLIES, cancel_refused=st.booleans())
def test_swap_leaves_no_gap(first: str, second: str, cancel_refused: bool) -> None:
    """HAT-EXEC-061: `gtt.swap` cancels the intraday stop only after the broker confirmed the
    GTT that replaces it; a GTT the broker refuses (after the one retry) or does not confirm
    leaves the intraday stop working, raises `gtt.swap_failed` and fails the job so it is tried
    again; an intraday stop the broker does not cancel also fails the job. Harm: the day stop is
    cancelled first and the position spends the night with no stop
    if the GTT fails."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(10, 40))
        rig.fake.script("place_gtt", _gtt_reply(first), _gtt_reply(second))
        if cancel_refused:
            rig.fake.script("cancel_order", {"s": "error", "message": "Order not found"})
        start = len(rig.fake.calls)
        report = rig.manager.swap_stops(session, rig.account_id, at(15, 25))
        names = [name for name, _ in _writes_since(rig, start)]
        working = {row.purpose for row in rig.stops(position.id)}
        if "cancel_order" in names:
            assert names.index("place_gtt") < names.index("cancel_order")
        confirmed = first == "accepted" or (first == "rejected" and second == "accepted")
        if confirmed and cancel_refused:
            assert working == {STOP_DAY, STOP_GTT}
            assert report.failed == [position.id]
        elif confirmed:
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


TICKS = st.sampled_from([D("0.01"), D("0.05"), D("0.10")])


@settings(max_examples=30, deadline=None)
@given(
    reply=st.sampled_from(["ok", "not_found", "stuck", "unavailable", "unread"]),
    raised=st.decimals(min_value=0, max_value=3, places=2),
    ticks=st.tuples(TICKS, TICKS),
)
@example(reply="ok", raised=D("0.03"), ticks=(D("0.01"), D("0.05")))
@example(reply="not_found", raised=D("0.03"), ticks=(D("0.01"), D("0.05")))
def test_verify_covers(reply: str, raised: Decimal, ticks: tuple[Decimal, Decimal]) -> None:
    """HAT-EXEC-063, HAT-OPS-024, HAT-EXEC-064: each night every open real position's GTT is
    refreshed to the current stop (`gtt.refreshed`), never below the trigger it rests at: when
    the instrument's tick changed since the GTT last moved, a coarser tick that floors the stop
    lower leaves the trigger where it is. A GTT whose refresh the broker refuses is cancelled,
    alerted (`gtt.missing`) and placed again; when the broker cannot be reached, the refused GTT
    cannot be cancelled, it was kept above the new tick's floor (a new one would be lower) or the
    fills cannot be read, the position is alerted (`gtt.unverified`) and no GTT is modified or
    placed, since the old GTT may still rest there and a second one would
    sell the shares twice. Harm: a GTT that triggered, expired or was never placed goes
    unnoticed and the position opens the next day without a stop; a verify that lowers a stop
    loosens it."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        rig.tick = ticks[0]
        position = _opened(rig, at(16, 0))
        if raised > 0:
            position.current_stop = D(position.current_stop) + raised
            session.commit()
            rig.desk.follow(session, position.id, at(16, 5))
        [old] = rig.stops(position.id)
        resting = D(old.trigger_price)
        refreshed = _event_count(session, old, "gtt.refreshed")
        rig.tick = ticks[1]  # the tick the verify reads (a tick-size change overnight)
        if reply != "ok":
            rig.fake.script("modify_gtt", UNAVAILABLE if reply == "unavailable" else NOT_FOUND)
        if reply == "stuck":
            rig.fake.script("cancel_gtt", NOT_FOUND)
        if reply == "unread":  # the trade book cannot be read: the open quantity may be stale
            rig.fake.script("fetch_tradebook", BrokerUnavailableError("Fyers tradebook failed"))
        places, modifies = rig.fake.count("place_gtt"), rig.fake.count("modify_gtt")
        rig.manager.verify_stops(session, rig.account_id, at(23, 0))
        goal = broker_trigger(D(position.current_stop), ticks[1])
        stops = rig.stops(position.id)
        kept = goal < resting
        if reply in ("unavailable", "stuck", "unread") or (reply == "not_found" and kept):
            assert ALERT_GTT_UNVERIFIED in rig.codes()
            assert rig.fake.count("place_gtt") == places
            assert reply != "unread" or rig.fake.count("modify_gtt") == modifies
            assert [row.id for row in stops] == [old.id]
            return
        [gtt] = stops
        assert gtt.purpose == STOP_GTT
        assert D(gtt.trigger_price) <= D(position.current_stop)
        if reply == "ok":
            assert D(gtt.trigger_price) == max(goal, resting)
            assert gtt.id == old.id
            assert _event_count(session, gtt, "gtt.refreshed") == refreshed + 1
        else:  # the old GTT is gone: the new one is placed at the current stop
            assert D(gtt.trigger_price) == goal
            assert ALERT_GTT_MISSING in rig.codes()
            assert gtt.id != old.id
            assert rig.fake.count("place_gtt") == places + 1
            session.refresh(old)
            assert old.status == "cancelled"
            names = rig.call_names()
            assert names.index("cancel_gtt") < len(names) - 1 - names[::-1].index("place_gtt")


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
# The reconciliation state the fill sync and the exits meet (D118): today's success, none ever,
# only yesterday's, today's success then a run that cannot read the book, or a mismatch.
RECONCILED = st.sampled_from(["today", "never", "yesterday", "failed", "mismatch"])


def _reconciled_as(rig: ManageRig, session: Session, state: str) -> None:
    if state == "never":
        rig.guard.reconciled_for = None
    elif state == "yesterday":
        rig.guard.reconciled_for = TRADE_DAY - timedelta(days=1)
    elif state == "failed":
        book = rig.fake.orders
        rig.fake.orders = ConnectionError("down")
        with pytest.raises(BrokerUnavailableError):
            rig.reconciler.run(session, rig.account_id)
        rig.fake.orders = book
    elif state == "mismatch":
        rig.guard.blocked_accounts.add(rig.account_id)
        rig.guard.blocked_bots.add(rig.world.bot_id)


@settings(max_examples=25, deadline=None)
@given(
    kill=KILLS,
    reconciled=RECONCILED,
    path=st.lists(st.decimals(min_value=90, max_value=140, places=1), min_size=1, max_size=6),
)
@example(kill="none", reconciled="never", path=[D(139)])
@example(kill="none", reconciled="yesterday", path=[D(95)])
def test_real_exits_never_blocked(kill: str, reconciled: str, path: list[Decimal]) -> None:
    """06 §6, HAT-EXEC-070, HAT-DEC-B17, D118: a real position's live exits come from the same
    exit stack as a shadow position's, and are sent as MARKET SELL CNC orders whatever kill
    switch or pause is on and whatever the reconciliation state (none today, only yesterday's, a
    failed run, a mismatch): the broker sees the same stop and sales as with today's success
    and no switch. The hard stop is left to the broker stop resting under the position. Harm: a
    tripped kill switch or an unreadable order book traps a position that its exits should
    sell, leaves a new fill without its stop, or HAT sells twice at the stop."""
    sales: list[list[tuple[str, int]]] = []
    for variant, state in (("none", "today"), (kill, reconciled)):
        with Session(memory_engine()) as session:
            rig = ManageRig(session, exits=exit_stack("partials"))
            outcome = rig.enter(at(10, 30))
            row = rig.entry_row(outcome.signal_id)
            assert row is not None
            rig.fill(row, row.qty, D(row.price))
            _reconciled_as(rig, session, state)
            rig.sync(at(10, 40))
            position = rig.position(outcome.signal_id)
            assert position is not None
            [stop] = rig.stops(position.id)  # the new fill rests on its stop
            assert stop.qty == position.qty_open
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
            calls = rig.fake.calls[start:]
            made_room = False
            for name, args in calls:
                if name == "cancel_order" or (name == "modify_order" and "quantity" in args[1]):
                    made_room = True
                elif name == "place_order" and args["type"] == "MARKET":
                    # the resting stop gave up the shares first (a broker holds them for it)
                    assert made_room
                    made_room = False
            orders = [args for name, args in calls if name == "place_order"]
            assert all(o["side"] == "SELL" and o["product"] == "CNC" for o in orders)
            assert all(o["type"] == "MARKET" for o in orders)
            sales.append([(o["type"], o["quantity"]) for o in orders])
    assert sales[1] == sales[0]


# ---------------------------------------------------------------------------------------
# HAT-EXEC-074: no sale reaches the broker from 15:30 on, however long the run takes
# ---------------------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(start=st.integers(min_value=0, max_value=9), took=st.integers(min_value=0, max_value=10))
def test_no_sale_after_close(start: int, took: int) -> None:
    """HAT-EXEC-074, HAT-DEC-B18: a sale is checked against the time it is sent at, not the
    time its run started: a run that starts at 15:2x and reaches the sale `took` minutes later
    sends it only before 15:30, else raises `exit.not_placed` and leaves the broker stop
    resting. With no clock injected, the time sent is the run's start plus the monotonic time
    it has taken. Harm: a slow close run sends market orders after the close."""
    readings = iter([0.0])  # the run's start, then every later reading `took` minutes on

    def ticker() -> float:
        return next(readings, took * 60.0)

    with Session(memory_engine()) as session:
        rig = ManageRig(session, exits=exit_stack("partials"), ticker=ticker)
        position = _opened(rig, at(10, 40))
        [stop] = rig.stops(position.id)
        began = at(15, 20 + start)
        view = exits.ExitView(price=D(position.avg_entry) * D("1.4"), days_held=1)
        readings = iter([0.0])
        rig.manager.on_price(session, position.id, view, began)
        sold = [a for n, a in rig.fake.calls if n == "place_order" and a["type"] == "MARKET"]
        in_time = 20 + start + took < 30
        assert bool(sold) == in_time
        if not in_time:
            assert ALERT_EXIT_NOT_PLACED in rig.codes()
            assert [row.id for row in rig.stops(position.id)] == [stop.id]


# ---------------------------------------------------------------------------------------
# 07 §4: an entry the broker ends unfilled ends its signal; nothing is protected
# ---------------------------------------------------------------------------------------

ENDINGS = {"cancelled": (1, lifecycle.CANCELLED), "expired": (7, lifecycle.CANCELLED)}
ENDINGS["rejected"] = (5, lifecycle.REJECTED)


@settings(max_examples=15, deadline=None)
@given(ending=st.sampled_from(sorted(ENDINGS)), lost=st.booleans())
def test_entry_ended_by_broker(ending: str, lost: bool) -> None:
    """07 §4, HAT-EXEC-041: an entry the broker ends with nothing filled ends its signal:
    Cancelled when it was cancelled or expired at the close, Rejected when the exchange rejected
    it after the broker took it; one whose send answer was lost and that is first found already
    ended ends Rejected when it is adopted. No position opens and no stop is placed. Harm: a
    dead entry keeps its signal in EntryPlaced or Triggered, holding one of the bot's slots."""
    status, state = ENDINGS[ending]
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        if lost:
            rig.fake.script("place_order", unconfirmed(landed=True))
            state = lifecycle.REJECTED
        outcome = rig.enter(at(10, 30))
        for book in rig.fake.book:
            book["status"] = status
        start = len(rig.fake.calls)
        rig.sync(at(14, 0))  # adopted, or moved, by the sync that first sees it ended
        signal = session.get(HatSignal, outcome.signal_id)
        assert signal is not None
        session.refresh(signal)
        assert signal.state == state
        assert journal.replay_state(session, signal.id) == state
        assert rig.position(outcome.signal_id) is None
        assert _writes_since(rig, start) == []


# ---------------------------------------------------------------------------------------
# HAT-EXEC-046 (PR 49): an entry nothing of which filled is cancelled on a failed break or
# after the timeout
# ---------------------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    closes=st.lists(st.integers(-30, 30), min_size=1, max_size=16),
    checks=st.lists(st.integers(0, 100), min_size=1, max_size=8, unique=True),
    timeout=st.integers(1, 90),
    filled_before=st.one_of(st.none(), st.integers(0, 7)),
    seen_first=st.booleans(),
    retried=st.booleans(),
)
@example([-5, 10], [1, 30], 60, None, False, False)  # the trigger bar's close is ignored
@example([0, 10, -1], [12, 13], 60, None, False, False)  # a later close below the level
@example([0], [59, 60], 60, None, False, False)  # exactly the timeout
@example([0, -1], [20], 60, 0, False, False)  # part filled: left working
@example([0, -1], [20], 60, 0, True, False)  # the order shows the part fill first
@example([0], [30], 60, None, False, True)  # an earlier rejected attempt is not the clock
def test_unfilled_entry_cancelled(
    closes: list[int],
    checks: list[int],
    timeout: int,
    filled_before: int | None,
    seen_first: bool,
    retried: bool,
) -> None:
    """HAT-EXEC-046 (PR 49, plan audit N1/G19): a real entry nothing of which has filled is
    asked to be cancelled at the broker exactly once, at the first check where a final bar
    later than the trigger bar closed below the signal's level or the timeout has passed since
    the entry was placed; then the fill sync ends its signal Cancelled with no position. The
    trigger bar's own close never counts, and an entry with a fill keeps its remainder working
    (the partial-entry policy), also while only its order's status shows the part fill
    (`seen_first`: the book read before the fill sync opened the position). The timeout runs
    from the working order, not an earlier attempt the broker rejected (`retried`). Harm: a
    failed break's LIMIT rests all day and fills as the price falls through the level, or a
    part-filled entry's remainder is cut short."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        # the 10:25 bar's final instant, in UTC: SQLite keeps the wall time it is given
        triggered = (at(10, 30) + timedelta(seconds=2)).astimezone(UTC)
        if retried:  # the first entry is rejected; the router's retry is the working order
            rig.fake.script("place_order", rejected("Insufficient margin"))
        outcome = rig.enter(triggered)
        rows = session.scalars(
            select(HatOrder).where(
                HatOrder.signal_id == outcome.signal_id, HatOrder.purpose == "entry"
            )
        ).all()
        entry = next(row for row in rows if row.status == "placed")
        assert entry.broker_order_id
        for row in rows:  # the router stamps the wall clock; this day is the rig's
            # an earlier attempt, stamped well before the working order (worst case)
            row.placed_at = triggered if row is entry else triggered - timedelta(minutes=45)
        assert len(rows) == 1 + retried
        session.commit()
        signal = session.get(HatSignal, outcome.signal_id)
        assert signal is not None
        level = D(signal.trigger_price)
        tick = D("0.05")
        # bar k starts at 10:25 + 5k minutes: bar 0 is the trigger bar
        bars = [
            Bar(TICKER, at(10, 25) + timedelta(minutes=5 * k), level, level, level, c, 1)
            for k, c in enumerate(level + tick * n for n in closes)
        ]
        watch = EntryWatch(timedelta(minutes=timeout))
        fed = 0
        cancel_at: int | None = None
        filled = False
        for n, minute in enumerate(sorted(checks)):
            now = triggered + timedelta(minutes=minute)
            while fed < len(bars) and bars[fed].final_at <= now:
                watch.on_bar(bars[fed])
                fed += 1
            if filled_before == n and cancel_at is None:
                if seen_first:  # the order shows the part fill; no position yet
                    entry.status = "partially_filled"
                    session.commit()
                else:
                    rig.fill(entry, 1, D(entry.price))
                    rig.sync(now)
                filled = True
            before = rig.fake.calls_named("cancel_order").count(entry.broker_order_id)
            due = [s for s, _ in watch.due(session, rig.account_id, now)]
            if filled:  # the desk itself refuses an entry with a fill, whoever asks
                assert due == []
                assert not rig.manager.cancel_unfilled_entry(session, outcome.signal_id, now)
            for signal_id in due:
                rig.manager.cancel_unfilled_entry(session, signal_id, now)
            asked = rig.fake.calls_named("cancel_order").count(entry.broker_order_id) - before
            broke = any(triggered < b.final_at <= now and b.close < level for b in bars)
            want = cancel_at is None and not filled and (broke or minute >= timeout)
            assert asked == int(want), (minute, broke)
            if want:
                cancel_at = n
            if not (filled and seen_first):  # a sync would read the book's status back
                rig.sync(now)
        session.refresh(signal)
        if cancel_at is not None:
            assert signal.state == lifecycle.CANCELLED
            assert rig.position(outcome.signal_id) is None
        elif filled:
            assert _book_row(rig, entry)["status"] == WORKING_AT_BROKER
        else:
            assert signal.state == lifecycle.ENTRY_PLACED


# ---------------------------------------------------------------------------------------
# HAT-EXEC-062: a GTT that triggers and fills only in part
# ---------------------------------------------------------------------------------------


# How a triggered GTT's sale may show the GTT's tag in the order and trade books: none (as Fyers
# was assumed to before GTTs were tagged), bare, as the GTT book shows it, or with Fyers' "<n>:"
# prefix ([verify on a live account] which, if any, Fyers uses), or the mark Fyers gives an
# untagged GTT ("2:GTTUntagged"), which is no tag.
CHILD_TAGS = st.sampled_from([None, "untagged", "bare", "gtt", "numbered"])


def _child_tag(gtt: HatOrder, shape: str | None) -> str | None:
    if shape == "other":  # another GTT's tag: some other signal's stop, not this one's
        return f"1:GTT{encode_tag(uuid.UUID(int=99), 'stop_gtt', 0)}"
    if shape == "untagged":
        return "2:GTTUntagged"
    prefix = {None: None, "bare": "", "gtt": "GTT", "numbered": "1:GTT"}[shape]
    return None if prefix is None else f"{prefix}{gtt.tag}"


@settings(max_examples=20, deadline=None)
@given(
    part=st.floats(min_value=0.1, max_value=0.9),
    ended=st.booleans(),
    child=CHILD_TAGS,
    stray=st.booleans(),
)
@example(part=0.5, ended=True, child="bare", stray=True)
def test_gtt_trigger_handled(part: float, ended: bool, child: str | None, stray: bool) -> None:
    """HAT-EXEC-062, -066, HAT-DEC-B17: a GTT that triggered and filled only in part is not
    resized or doubled while its sale may still sell the rest (the only write is asking that sale
    to go at the market); once that sale ends short, `gtt.unfilled` is raised and the shares left
    get a new stop for exactly their quantity. The sale is the GTT's whether it carries the GTT's
    tag (bare or as Fyers shows a GTT's) or none; another untagged sale on the symbol that ended
    (`stray`, not at the GTT's quantity) changes nothing. Harm: a second stop under a sale still
    working sells the shares twice, the rest stays unprotected, or an unrelated cancelled sale
    ends the GTT and a second stop is placed while it still rests."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [gtt] = rig.stops(position.id)
        sold = max(1, int(gtt.qty * part))
        tag = _child_tag(gtt, child)
        order = {
            "id": "X-1",
            "orderTag": tag,
            "symbol": SYMBOLS["SBIN"],
            "side": -1,
            "qty": gtt.qty,
            "filledQty": sold,
            "status": 6,
            "productType": "CNC",
            "type": 1,
            "limitPrice": float(gtt.price),
        }
        # someone else's untagged sale on the symbol, cancelled unfilled
        other = {**order, "id": "X-9", "orderTag": None, "qty": gtt.qty + 1, "filledQty": 0}
        rig.fake.orders = [order, {**other, "status": 1}] if stray else [order]
        rig.fake.tradebook = [
            *rig.fake.tradebook,  # type: ignore[misc]
            {
                "tradeNumber": "T-G1",
                "orderNumber": "X-1",
                "orderTag": tag,
                "symbol": SYMBOLS["SBIN"],
                "side": -1,
                "tradedQty": sold,
                "tradePrice": float(D(gtt.trigger_price) - 1),
            },
        ]
        start = len(rig.fake.calls)
        rig.sync(at(10, 0) + timedelta(days=1))  # first sight: it may still fill
        rig.sync(at(10, 1) + timedelta(days=1))
        session.refresh(position)
        assert position.qty_open == gtt.qty - sold
        assert _writes_since(rig, start) == [("modify_order", ("X-1", {"order_type": "MARKET"}))]
        if not ended:
            return
        order["status"] = 1  # the rest of the sale was cancelled
        rig.sync(at(10, 5) + timedelta(days=1))
        assert rig.codes().count(ALERT_GTT_UNFILLED) == 2  # resting, then ended short
        [stop] = [row for row in rig.stops(position.id) if row.id != gtt.id]
        assert stop.qty == position.qty_open
        rig.sync(at(10, 10) + timedelta(days=1))
        assert rig.codes().count(ALERT_GTT_UNFILLED) == 2


def _gtt_alerts(rig: ManageRig, code: str) -> int:
    return sum(
        1 for name, _, p in rig.notifier.alerts if name == ALERT_GTT_UNFILLED and p["code"] == code
    )


@settings(max_examples=30, deadline=None)
@given(
    part=st.floats(min_value=0.0, max_value=0.9),
    refusals=st.integers(min_value=0, max_value=3),
    syncs=st.integers(min_value=1, max_value=4),
    fills=st.booleans(),
    exits=st.booleans(),
    foreign=st.booleans(),
    child=st.one_of(CHILD_TAGS, st.just("other")),
)
@example(part=0.0, refusals=0, syncs=2, fills=True, exits=True, foreign=False, child=None)
@example(part=0.5, refusals=3, syncs=4, fills=False, exits=True, foreign=False, child=None)
@example(part=0.0, refusals=0, syncs=2, fills=True, exits=False, foreign=True, child=None)
@example(part=0.0, refusals=0, syncs=2, fills=True, exits=False, foreign=True, child="numbered")
@example(part=0.0, refusals=0, syncs=2, fills=True, exits=False, foreign=False, child="other")
@example(part=0.0, refusals=0, syncs=2, fills=True, exits=False, foreign=False, child="untagged")
def test_gtt_resting_marketed(
    part: float,
    refusals: int,
    syncs: int,
    fills: bool,
    exits: bool,
    foreign: bool,
    child: str | None,
) -> None:
    """HAT-EXEC-066, HAT-EXEC-062: a GTT that triggered while the price was below its limit
    leaves its limit sale working at the broker. The fill sync records `gtt.triggered` and raises
    `gtt.unfilled` (code `gtt_triggered_resting`) once, and asks the broker to make that order a
    market sale (exactly `{order_type: MARKET}` on the order's id) at every sync until the book
    shows it as one; a refused conversion is alerted once and asked again at the next sync. A
    sale first seen is left a minute to fill at its limit. A sale under the GTT's tag (bare or as
    Fyers shows a GTT's) is the GTT's at any price; an untagged sale at another price, or one
    under another GTT's tag, is not the GTT's and is never touched. No other order, stop or GTT
    is sent while the sale works (a flatten waits for it, a raised stop leaves the triggered GTT
    where it is); once it
    fills the position closes with no stop left, and a trigger of an earlier day whose sale the
    book no longer shows holds no exit back. Harm: the stop triggered and the shares sit unsold
    below the limit all session while the price keeps falling, a second sale or stop sells the
    shares twice, or an order HAT did not place is dumped at the market."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [gtt] = rig.stops(position.id)
        sold = int(gtt.qty * part)
        tag = _child_tag(gtt, child)
        # not the GTT's: another GTT's tag, or untagged at another price than the GTT's limit
        not_ours = child == "other" or (foreign and child in (None, "untagged"))
        order = {
            "id": "X-1",
            "orderTag": tag,
            "symbol": SYMBOLS["SBIN"],
            "side": -1,
            "qty": gtt.qty,
            "filledQty": sold,
            "status": 6,
            "productType": "CNC",
            "type": 1,
            "limitPrice": float(D(gtt.price) - (TICK if foreign else 0)),
        }
        rig.fake.orders = [order]
        if sold:
            rig.fake.tradebook = [
                *rig.fake.tradebook,  # type: ignore[misc]
                {
                    "tradeNumber": "T-G1",
                    "orderNumber": "X-1",
                    "orderTag": tag,
                    "symbol": SYMBOLS["SBIN"],
                    "side": -1,
                    "tradedQty": sold,
                    "tradePrice": float(D(gtt.trigger_price) - 1),
                },
            ]
        refused = min(refusals, syncs)
        rig.fake.script("modify_order", *([UNAVAILABLE] * refused))
        start = len(rig.fake.calls)
        rig.sync(at(9, 15) + timedelta(days=1))  # first sight: it may still fill at its limit
        assert _writes_since(rig, start) == []
        assert _event_count(session, gtt, EVENT_GTT_TRIGGERED) == 0
        if not_ours:  # not the GTT's sale: never touched, never alerted
            for n in range(syncs):
                rig.sync(at(9, 16 + n) + timedelta(days=1))
            assert _writes_since(rig, start) == []
            assert _gtt_alerts(rig, RESTING) == 0
            return
        for n in range(syncs):
            when = at(9, 16 + n) + timedelta(days=1)
            rig.sync(when)
            if exits:
                position.current_stop = D(position.current_stop) + TICK
                session.commit()
                rig.desk.follow(session, position.id, when)
                rig.manager.flatten(session, rig.account_id, when)
        writes = _writes_since(rig, start)
        asked = refused + (1 if syncs > refused else 0)
        assert writes == [("modify_order", ("X-1", {"order_type": "MARKET"}))] * asked
        assert _event_count(session, gtt, EVENT_GTT_TRIGGERED) == 1
        assert _gtt_alerts(rig, RESTING) == 1
        assert _gtt_alerts(rig, NOT_MARKETED) == (1 if refused else 0)
        session.refresh(position)
        assert position.qty_open == gtt.qty - sold
        if not fills or syncs == refused:
            rig.fake.orders = []  # the next day's book: the sale expired unseen
            start = len(rig.fake.calls)
            rig.manager.flatten(session, rig.account_id, at(10, 0) + timedelta(days=2))
            assert "place_order" in [name for name, _ in _writes_since(rig, start)]
            return
        order.update(filledQty=gtt.qty, status=2)  # the market sale sold the rest
        rig.fake.tradebook = [
            *rig.fake.tradebook,  # type: ignore[misc]
            {
                "tradeNumber": "T-G2",
                "orderNumber": "X-1",
                "orderTag": tag,
                "symbol": SYMBOLS["SBIN"],
                "side": -1,
                "tradedQty": gtt.qty - sold,
                "tradePrice": float(D(gtt.trigger_price) - 3),
            },
        ]
        start = len(rig.fake.calls)
        rig.sync(at(9, 30) + timedelta(days=1))
        session.refresh(position)
        assert position.qty_open == 0
        assert position.closed_at is not None
        assert [name for name, _ in _writes_since(rig, start)] == []
        assert rig.stops(position.id, *LIVE) == []


@settings(max_examples=20, deadline=None)
@given(
    extra=st.integers(min_value=3, max_value=40),
    parts=st.integers(min_value=1, max_value=3),
    syncs=st.integers(min_value=1, max_value=3),
)
@example(extra=3, parts=1, syncs=2)
@example(extra=3, parts=3, syncs=1)
def test_a_second_order_under_one_tag_is_tracked(extra: int, parts: int, syncs: int) -> None:
    """HAT-BRK-103, HAT-DEC-B17: when the broker holds a second order under an entry's tag (Fyers
    does not enforce tag uniqueness) and it fills, every share is recorded on the position, its
    stop covers them all, and `fill.excess` is raised once, however many partial fills make up
    the excess and however many syncs follow, only once the stop covers every share. Harm:
    shares bought twice sit at the broker untracked and with no stop, or the owner never learns
    the position is larger than its risk budget."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        row = rig.entry_row(position.signal_id)
        assert row is not None
        rig.fake.book.append({**rig.fake.book[0], "id": "DUP-1", "qty": extra, "filledQty": extra})
        rig.fake.tradebook = [
            *rig.fake.tradebook,  # type: ignore[misc]
            *(
                {
                    "tradeNumber": f"T-DUP-{n}",
                    "orderNumber": "DUP-1",
                    "orderTag": row.tag,
                    "symbol": SYMBOLS["SBIN"],
                    "side": 1,
                    "tradedQty": extra // parts + (extra % parts if n == 0 else 0),
                    "tradePrice": float(D(row.price)),
                }
                for n in range(parts)
            ),
        ]
        covered: list[bool] = []
        alert = rig.notifier.alert

        def checked(code: str, day: date, detail: dict) -> None:
            if code == ALERT_FILL_EXCESS:
                session.refresh(position)
                live = sum(stop.qty for stop in rig.stops(position.id, *LIVE))
                covered.append(live == position.qty_open)
            alert(code, day, detail)

        rig.notifier.alert = checked  # type: ignore[method-assign]
        for n in range(syncs):
            rig.sync(at(16, 5 + n))
        session.refresh(position)
        assert position.qty_open == row.qty + extra
        assert covered == [True]
        assert rig.codes().count(ALERT_FILL_EXCESS) == 1
        live = rig.stops(position.id, *LIVE)
        assert sum(stop.qty for stop in live) == position.qty_open


@settings(max_examples=20, deadline=None)
@given(status=st.sampled_from([1, 5, 7]), before=st.booleans(), syncs=st.integers(1, 3))
@example(status=5, before=False, syncs=1)
def test_a_dead_second_order_never_ends_the_entry(status: int, before: bool, syncs: int) -> None:
    """HAT-BRK-103: a second order under a filled entry's tag that the broker cancels, rejects or
    expires with nothing filled, listed before or after the entry's own order, leaves the entry
    row on its own broker order id and status, its signal as it was and the position open under
    its stops. Harm: the duplicate's status is read as the entry's, and an open position's
    signal is ended or its order row points at an order HAT never tracked."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        row = rig.entry_row(position.signal_id)
        assert row is not None
        signal = session.get(HatSignal, position.signal_id)
        assert signal is not None
        state, order_id, row_status = signal.state, row.broker_order_id, row.status
        dead = {**rig.fake.book[0], "id": "DUP-1", "status": status, "filledQty": 0}
        rig.fake.book = [dead, *rig.fake.book] if before else [*rig.fake.book, dead]
        for n in range(syncs):
            rig.sync(at(16, 5 + n))
        session.refresh(row)
        session.refresh(signal)
        session.refresh(position)
        assert (row.broker_order_id, row.status) == (order_id, row_status)
        assert signal.state == state
        assert position.closed_at is None
        assert sum(stop.qty for stop in rig.stops(position.id, *LIVE)) == position.qty_open


# ---------------------------------------------------------------------------------------
# HAT-EXEC-065: a lost broker answer never freezes a position
# ---------------------------------------------------------------------------------------

LOST_GTT = st.sampled_from(["unconfirmed", "unconfirmed_landed", "raises"])
LOST_SALE = st.sampled_from(["unconfirmed", "unconfirmed_landed", "invalid"])


def _resting(rig: ManageRig, position: HatPosition) -> bool:
    """A stop the broker confirmed rests under the position."""
    return any(row.broker_order_id for row in rig.stops(position.id, *LIVE))


def _sale_working(rig: ManageRig, position: HatPosition) -> bool:
    """A sale of the position works at the broker (its row is in the order book, not ended)."""
    tags = set(
        rig.session.scalars(
            select(HatOrder.tag).where(
                HatOrder.position_id == position.id,
                HatOrder.purpose.in_(("exit_partial", "exit_full", "flatten")),
            )
        )
    )
    return any(row["orderTag"] in tags and row["status"] in (4, 6) for row in rig.fake.book)


def _invalid_once(rig: ManageRig) -> None:
    """The adapter refuses the next sale it is asked to send (before any SDK call)."""
    real = rig.broker._order_payload
    calls = iter([True])

    def payload(intent: Any) -> dict[str, Any]:  # noqa: ANN401 - the adapter's OrderIntent
        if intent.order_type == "MARKET" and next(calls, False):
            raise InvalidIntentError()
        return real(intent)

    rig.broker._order_payload = payload  # type: ignore[method-assign]


def _sold(rig: ManageRig, start: int) -> list[dict[str, Any]]:
    return [a for n, a in rig.fake.calls[start:] if n == "place_order" and a["type"] == "MARKET"]


@settings(max_examples=30, deadline=None)
@given(
    path=st.sampled_from(["late_fill", "swap"]), gtt=LOST_GTT, sale=LOST_SALE, young=st.booleans()
)
@example(path="late_fill", gtt="unconfirmed", sale="unconfirmed", young=False)
@example(path="swap", gtt="unconfirmed_landed", sale="unconfirmed_landed", young=False)
def test_lost_answers_never_freeze(path: str, gtt: str, sale: str, young: bool) -> None:
    """HAT-EXEC-065, HAT-DEC-B17: a GTT whose placement answer was lost (timed out, raised,
    id-less), placed for a fill after 15:25 or by the 15:25 swap, is settled against the
    broker's GTT book by the next sync or swap: adopted when it landed, else ended and placed
    again, so the position rests on a stop the broker confirmed. Until it is settled (sent too
    recently: `young`) nothing is placed on top of it and it never holds a sale back. A sale
    whose answer was lost, or that the adapter refused, never blocks the next one: after a
    sync, a second flatten either sells or finds a sale working at the broker. Harm: a lost
    answer leaves a position that HAT can neither protect nor sell, even on /flatten."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session, settle_after=timedelta(hours=1) if young else timedelta(0))
        if path == "late_fill":
            rig.fake.script("place_gtt", _gtt_reply(gtt))
            position = _opened(rig, at(15, 26))
            rig.sync(at(15, 27))
        else:
            position = _opened(rig, at(10, 40))
            rig.fake.script("place_gtt", _gtt_reply(gtt))
            rig.manager.swap_stops(session, rig.account_id, at(15, 25))
            rig.manager.swap_stops(session, rig.account_id, at(15, 26))
        landed = gtt == "unconfirmed_landed"
        if young:
            # Not settled yet: no second GTT on top of one that may rest at the broker, and no
            # new alert on each sync while its answer may still be in flight.
            alerts = rig.codes().count(ALERT_STOP_NOT_PLACED)
            rig.sync(at(15, 27))
            rig.sync(at(15, 27))
            assert rig.fake.count("place_gtt") == 1
            assert rig.codes().count(ALERT_STOP_NOT_PLACED) == alerts
        else:
            assert _resting(rig, position)
            assert rig.fake.count("place_gtt") == (1 if landed else 2)
            assert len(rig.fake.resting_gtts()) == 1  # one GTT rests at the broker, never two
            if path == "swap":
                assert {row.purpose for row in rig.stops(position.id)} == {STOP_GTT}
        if sale == "invalid":
            _invalid_once(rig)
        else:
            rig.fake.script("place_order", unconfirmed(landed=sale == "unconfirmed_landed"))
        start = len(rig.fake.calls)
        rig.manager.flatten(session, rig.account_id, at(15, 28))
        assert _sold(rig, start) or sale == "unconfirmed" or sale == "unconfirmed_landed"
        rig.sync(at(15, 28))
        session.refresh(position)
        if position.closed_at is None:
            start = len(rig.fake.calls)
            rig.manager.flatten(session, rig.account_id, at(15, 29))
            assert _sold(rig, start) or _sale_working(rig, position) or young


# ---------------------------------------------------------------------------------------
# HAT-EXEC-075, HAT-OPS-032: a sale the broker took and then rejected is retried once, alerted
# ---------------------------------------------------------------------------------------

SALE_ENDINGS = {"rejected": 5, "cancelled": 1, "expired": 7}


@settings(max_examples=25, deadline=None)
@given(
    ending=st.sampled_from(sorted(SALE_ENDINGS)),
    part=st.integers(min_value=0, max_value=2),
    again=st.booleans(),
)
@example(ending="rejected", part=0, again=True)
@example(ending="cancelled", part=1, again=False)
def test_async_sale_rejection_retried(ending: str, part: int, again: bool) -> None:
    """HAT-EXEC-075, HAT-OPS-032: a sale the broker accepted and then ended short (rejected by
    the exchange or RMS, cancelled, expired) records `order.rejected` (relayed as the health
    alert), fires its rule again and is sent once more under the next tag for what is still
    open: the full exit stays a full exit, never lost to a later partial. A second such ending
    raises `exit.not_placed` and nothing more is sent that session; the position is protected
    again each time. Harm: a sale the broker drops leaves the position unsold with its rule
    spent, silently. After every step the signal's stored state is the fold of its events
    (HAT-EXEC-042, PR 36): the retry's `order.placed` while the trade is already ExitPending is
    kept as a non-step. Harm: the log and the trade table disagree once a sale is retried.
    (Orders are stamped on the rig's trade day, so the session's rejections are counted on
    it.)"""
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", return_value=at(11, 0)),
    ):
        rig = ManageRig(session, exits=exit_stack("partials"))
        position = _opened(rig, at(10, 40))
        signal = session.get(HatSignal, position.signal_id)
        assert signal is not None

        def replays() -> None:
            session.refresh(signal)
            session.refresh(position)
            assert journal.replay_state(session, signal.id) == signal.state
            assert position.state == signal.state

        view = exits.ExitView(price=D(position.avg_entry) * D("1.4"), days_held=1)

        def end_sales(minute: int) -> list[HatOrder]:
            rows = list(
                session.scalars(
                    select(HatOrder).where(
                        HatOrder.position_id == position.id, HatOrder.purpose == "exit_full"
                    )
                )
            )
            for row in rows:
                book = next(b for b in rig.fake.book if b["orderTag"] == row.tag)
                if book["status"] in (4, 6):
                    if part:
                        rig.fill(row, part, D(row.qty and position.avg_entry))
                    book["status"] = SALE_ENDINGS[ending]
            rig.sync(at(11, minute))
            return rows

        start = len(rig.fake.calls)
        rig.manager.on_price(session, position.id, view, at(11, 0))
        first = _sold(rig, start)
        assert [o["quantity"] for o in first] == [500]
        replays()
        [row] = end_sales(1)
        replays()
        session.refresh(position)
        assert _event_count(session, row, "order.rejected") == 1
        assert _resting(rig, position)
        start = len(rig.fake.calls)
        rig.manager.on_price(session, position.id, view, at(11, 2))
        retry = _sold(rig, start)
        assert [o["quantity"] for o in retry] == [position.qty_open]
        assert retry[0]["order_tag"] != first[0]["order_tag"]
        replays()
        if again:
            end_sales(3)
            replays()
            assert ALERT_EXIT_NOT_PLACED in rig.codes()
            start = len(rig.fake.calls)
            rig.manager.on_price(session, position.id, view, at(11, 4))
            assert _sold(rig, start) == []
            session.refresh(position)
            assert _resting(rig, position)
            replays()


@settings(max_examples=20, deadline=None)
@given(
    parts=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3),
    ends=st.booleans(),
)
@example(parts=[0], ends=False)
def test_a_resting_sale_is_never_stopped_over(parts: list[int], ends: bool) -> None:
    """HAT-EXEC-060, HAT-EXEC-065: while a full sale the broker accepted still works (resting,
    or part filled), no sync places or grows a stop over the shares it sells: `make_room`
    cancelled the stops for it, and a new stop would hold the shares and the sale be refused.
    Once the sale ends short, the next sync protects what is left. Harm: a stop rests under
    shares a working sale is selling, so the sale is refused or the position sold twice."""
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", return_value=at(11, 0)),
    ):
        rig = ManageRig(session, exits=exit_stack("partials"))
        position = _opened(rig, at(10, 40))
        view = exits.ExitView(price=D(position.avg_entry) * D("1.4"), days_held=1)
        start = len(rig.fake.calls)
        rig.manager.on_price(session, position.id, view, at(11, 0))
        [sale] = _sold(rig, start)
        [row] = _sale_rows(rig, position)
        after = len(rig.fake.calls)
        for minute, part in enumerate(parts, start=1):
            if part:
                rig.fill(row, part, D(position.avg_entry))
            rig.sync(at(11, minute))
            stops = [n for n, a in rig.fake.calls[after:] if n in STOP_WRITES and a is not sale]
            assert stops == []
            assert rig.stops(position.id, *LIVE) == []
        if ends:
            book = next(b for b in rig.fake.book if b["orderTag"] == row.tag)
            book["status"] = 5
            rig.sync(at(11, 10))
            session.refresh(position)
            assert _resting(rig, position)


def _sale_rows(rig: ManageRig, position: HatPosition) -> list[HatOrder]:
    rows = list(
        rig.session.scalars(
            select(HatOrder)
            .where(
                HatOrder.position_id == position.id,
                HatOrder.purpose.in_(("exit_partial", "exit_full", "flatten")),
            )
            .order_by(HatOrder.tag)
        )
    )
    for row in rows:
        rig.session.refresh(row)
    return rows


@settings(max_examples=20, deadline=None)
@given(
    case=st.sampled_from(["closed_sweep", "landed_filled", "earlier_session"]),
    path=st.sampled_from(["late_fill", "swap"]),
)
def test_a_settled_answer_never_sells_twice(case: str, path: str) -> None:
    """HAT-EXEC-065: settling a lost answer never leaves a second seller behind. A GTT whose
    answer was lost and that landed, still unsettled when a sale closes the position, is
    cancelled at the broker by the first sync after the settle wait (`closed_sweep`). A lost
    sale that the broker filled is left to the fill sync, which records its fills, and no
    second sale goes out meanwhile (`landed_filled`). A lost sale of an earlier session that
    today's book does not hold may have sold: it is left unsure with its rule fired, alerted,
    and nothing more is sold while a broker stop keeps protecting the position
    (`earlier_session`). Harm: a GTT rests under shares already sold, or a position is sold
    twice."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session, exits=exit_stack("partials"), settle_after=timedelta(minutes=1))
        if case == "closed_sweep":
            if path == "late_fill":
                rig.fake.script("place_gtt", unconfirmed(landed=True))
                position = _opened(rig, at(15, 26))
            else:
                position = _opened(rig, at(10, 40))
                rig.fake.script("place_gtt", unconfirmed(landed=True))
                rig.manager.swap_stops(session, rig.account_id, at(15, 25))
            assert len(rig.fake.resting_gtts()) == 1
            rig.manager.flatten(session, rig.account_id, at(15, 28))
            [sale] = _sale_rows(rig, position)
            rig.fill(sale, sale.qty, D(position.avg_entry))
            rig.sync(at(15, 28))
            session.refresh(position)
            assert position.closed_at is not None
            rig.elapsed = timedelta(minutes=2)
            rig.sync(at(15, 29))
            assert rig.fake.resting_gtts() == []
        elif case == "landed_filled":
            position = _opened(rig, at(15, 26) if path == "late_fill" else at(10, 40))
            rig.fake.script("place_order", unconfirmed(landed=True))
            rig.manager.flatten(session, rig.account_id, at(15, 27))
            [sale] = _sale_rows(rig, position)
            rig.fill(sale, sale.qty, D(position.avg_entry))
            rig.elapsed = timedelta(minutes=2)
            start = len(rig.fake.calls)
            rig.manager.flatten(session, rig.account_id, at(15, 28))
            assert _sold(rig, start) == []
            rig.sync(at(15, 29))
            session.refresh(position)
            assert position.closed_at is not None
            assert position.qty_open == 0
        else:
            position = _opened(rig, at(10, 40))
            view = exits.ExitView(price=D(position.avg_entry) * D("1.4"), days_held=1)
            rig.fake.script("place_order", unconfirmed())
            rig.manager.on_price(session, position.id, view, at(11, 0))
            [sale] = _sale_rows(rig, position)
            assert sale.rule_ids
            rig.elapsed = timedelta(days=4)
            start = len(rig.fake.calls)
            rig.manager.flatten(session, rig.account_id, at(11, 5))
            rig.sync(at(11, 6))
            session.refresh(position)
            session.refresh(sale)
            assert sale.status == "unconfirmed"
            assert set(sale.rule_ids) <= set(position.rules_fired or [])
            reasons = [p["reason"] for c, _, p in rig.notifier.alerts if c == ALERT_EXIT_NOT_PLACED]
            assert "sale_unresolved" in reasons
            assert _sold(rig, start) == []
            assert _resting(rig, position)


@settings(max_examples=20, deadline=None)
@given(
    order=st.sampled_from(["entry", "sale"]),
    landed=st.booleans(),
    young=st.booleans(),
)
@example(order="sale", landed=False, young=True)
@example(order="entry", landed=False, young=True)
def test_the_close_ends_only_settled_answers(order: str, landed: bool, young: bool) -> None:
    """HAT-EXEC-065, HAT-EXEC-074: after the close, an entry or a sale whose answer was lost
    and that the broker's book does not hold ends (an entry's signal Rejected, a sale
    `expired`); one the book holds is left for the sync to adopt, and one sent less than the
    settle wait ago is left for the next run, since it may still be on its way. Harm: an order
    that reached the broker is written off, so HAT loses track of a real position or sells it
    again."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session, settle_after=timedelta(hours=1) if young else timedelta(0))
        if order == "entry":
            rig.fake.script("place_order", unconfirmed(landed=landed))
            outcome = rig.enter(at(10, 30))
            row = rig.entry_row(outcome.signal_id)
        else:
            position = _opened(rig, at(10, 40))
            rig.fake.script("place_order", unconfirmed(landed=landed))
            rig.manager.flatten(session, rig.account_id, at(11, 0))
            [row] = _sale_rows(rig, position)
        assert row is not None
        session.refresh(row)
        assert row.status == "unconfirmed"
        rig.manager.end_session(session, rig.account_id, at(15, 40))
        session.refresh(row)
        signal = session.get(HatSignal, row.signal_id)
        assert signal is not None
        if landed or young:
            assert row.status == "unconfirmed"
            assert signal.state != "Rejected"
        else:
            assert row.status == "expired"
            assert order == "sale" or signal.state == "Rejected"


# ---------------------------------------------------------------------------------------
# HAT-EXEC-144: /flatten of a position spanning an unhandled action sells what the broker holds
# ---------------------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    action=st.sampled_from([None, ("other", "0.95"), ("split", "10"), ("bonus", "0.5")]),
    held=st.one_of(st.none(), st.integers(min_value=0, max_value=900)),
    days=st.integers(min_value=1, max_value=2),
    twin=st.booleans(),
)
@example(action=("split", "10"), held=1, days=1, twin=False)
@example(action=("other", "0.95"), held=None, days=2, twin=False)
@example(action=("split", "10"), held=600, days=1, twin=True)
def test_flatten_capped_at_holdings(
    action: tuple[str, str] | None, held: int | None, days: int, twin: bool
) -> None:
    """HAT-EXEC-144: a position spanning an action the rescale cannot handle (a consolidation
    leaves fewer shares than HAT holds) is flattened for at most the shares the broker holds of
    the symbol; a readable book of none sells nothing, an unreadable one sells the open
    quantity (a flatten is never blocked). Two positions of the symbol (`twin`) share the held
    shares: together they sell at most what is held. Every other position, one spanning a
    bonus (an action the rescale handles) included, is sold for its open quantity whatever the
    book says. Harm: an oversell the broker refuses leaves the position unsold, or one sale is
    short of what HAT holds."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(10, 40))
        total = position.qty_open
        if twin:  # a second open position of the symbol, as a second bot of the account holds
            first = session.get(HatSignal, position.signal_id)
            columns = HatSignal.__table__.columns
            signal = HatSignal(**{c.key: getattr(first, c.key) for c in columns if c.key != "id"})
            signal.id = uuid.uuid4()
            signal.trade_date -= timedelta(days=1)  # one signal per bot, playbook, name and day
            session.add(signal)
            session.flush()
            session.add(
                HatPosition(
                    bot_id=position.bot_id,
                    signal_id=signal.id,
                    instrument_id=position.instrument_id,
                    is_virtual=False,
                    qty_open=position.qty_open,
                    avg_entry=position.avg_entry,
                    initial_stop=position.initial_stop,
                    current_stop=position.current_stop,
                    currency=position.currency,
                    opened_at=position.opened_at,
                    state="Open",
                    rules_fired=[],
                )
            )
            session.commit()
            total *= 2
        if action is not None:
            session.add(
                HatCorporateAction(
                    instrument_id=position.instrument_id,
                    ex_date=TRADE_DAY + timedelta(days=1),
                    action_type=action[0],
                    adjustment_factor=D(action[1]),
                    raw_text="fixture",
                    source="mcp",
                )
            )
            session.commit()
        rig.fake.holdings = (
            BrokerUnavailableError("broker_unavailable")
            if held is None
            else [{"symbol": SYMBOLS["SBIN"], "quantity": held, "costPrice": 100}]
        )
        start = len(rig.fake.calls)
        rig.manager.flatten(session, rig.account_id, at(10, 0) + timedelta(days=days))
        sold = sum(int(a["quantity"]) for a in _sold(rig, start))
        unhandled = action is not None and action[0] != "bonus"
        if unhandled and held is not None:
            assert sold == min(total, held)
        else:
            assert sold == total


# ---------------------------------------------------------------------------------------
# HAT-EXEC-145, -146, -063 (PR 47): GTTs around an ex-date, a series change, an old GTT
# ---------------------------------------------------------------------------------------

# How the GTT book shows a GTT the broker cancelled: ASSUMED until the O2(b) live check.
ASSUMED_SHAPES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fyers_gtt_ex_date_assumed.json").read_text("utf-8")
)["shapes"]
DAYS = [d for d in CALENDAR.trading_days() if d > TRADE_DAY][:3]
CHECK_DAY = DAYS[0]  # the trading day after the entry


def _on(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def _book_shape(rig: ManageRig, row: HatOrder, shape: str) -> None:
    """The broker's GTT book after the night: the GTT resting, or one assumed shape of a GTT the
    broker cancelled (`fyers_gtt_ex_date_assumed.json`)."""
    if shape == "resting":
        return
    patch_row = ASSUMED_SHAPES[shape]
    if patch_row is None:
        rig.fake.gtts = [g for g in rig.fake.gtts if g["id"] != row.broker_order_id]
        return
    for gtt in rig.fake.gtts:
        if gtt["id"] == row.broker_order_id:
            gtt.update(patch_row)


def _read_book(rig: ManageRig, start: int) -> bool:
    return any(name == "fetch_gtt_orders" for name, _ in rig.fake.calls[start:])


@settings(max_examples=40, deadline=None)
@given(
    ex=st.sampled_from([None, -1, 0, 1, 2]),  # the action's ex-date, in trading days from today
    kind=st.sampled_from([TYPE_DIVIDEND, TYPE_NOTICE, TYPE_SPLIT]),
    shape=st.sampled_from(["resting", "cancelled", "dropped", "unknown_status"]),
    refused=st.booleans(),  # the broker refuses a GTT before the open
)
@example(ex=0, kind=TYPE_DIVIDEND, shape="cancelled", refused=False)
@example(ex=1, kind=TYPE_DIVIDEND, shape="dropped", refused=True)
@example(ex=0, kind=TYPE_SPLIT, shape="cancelled", refused=False)
@example(ex=2, kind=TYPE_DIVIDEND, shape="cancelled", refused=False)
def test_ex_date_gtt_replaced(ex: int | None, kind: str, shape: str, refused: bool) -> None:
    """HAT-EXEC-145 (PR 47, research G01): at 08:40 every open real position with a stored action
    (a dividend's too) of ex-date today or the next session has its GTT checked against the
    broker's GTT book. One the book no longer holds (cancelled, or gone) is ended `cancelled`,
    alerted `gtt.missing` and placed again at the current stop; a GTT the broker refuses before
    the open is alerted (`stop.not_placed`), does not fail the run, and the fill sync places
    the intraday stop from 09:15. A GTT still resting is left alone; a book that cannot be read
    is alerted `gtt.unverified`, nothing is placed and the run fails (tried again); a position
    still awaiting a split's rescale gets nothing until it is rescaled; a position with no
    action that near is never looked at. Harm: Fyers cancels the GTT the night before a
    dividend and the position opens the ex-date with no stop at the broker."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        if ex is not None:
            ex_day = TRADE_DAY if ex == -1 else DAYS[ex]
            factor = D("0.5") if kind == TYPE_SPLIT else D(1)
            session.add(
                HatCorporateAction(
                    instrument_id=position.instrument_id,
                    ex_date=ex_day,
                    action_type=kind,
                    adjustment_factor=factor,
                    raw_text="fixture",
                    source="mcp",
                )
            )
            session.commit()
        _book_shape(rig, old, shape)
        if refused:
            rig.fake.script("place_gtt", rejected("GTT refused"), rejected("GTT refused"))
        start, alerts = len(rig.fake.calls), len(rig.codes())
        report = rig.manager.check_ex_date_gtts(session, rig.account_id, _on(CHECK_DAY, 8, 40))
        new_alerts = rig.codes()[alerts:]
        writes = _writes_since(rig, start)
        near = ex in (0, 1)
        if not near:
            assert not _read_book(rig, start)
            assert (writes, report.done, report.failed, new_alerts) == ([], 0, [], [])
            return
        session.refresh(old)
        if shape == "resting":
            assert (writes, report.done, report.failed) == ([], 1, [])
            assert old.status in LIVE
            return
        if shape == "unknown_status":  # the book cannot be read: nothing placed, tried again
            assert writes == []
            assert ALERT_GTT_UNVERIFIED in new_alerts
            assert report.failed == [position.id]
            assert old.status in LIVE
            return
        if kind == TYPE_SPLIT and ex == 0:  # awaits its rescale: nothing at the old scale
            assert (writes, report.failed) == ([], [position.id])
            return
        assert old.status == "cancelled"
        assert ALERT_GTT_MISSING in new_alerts
        assert report.failed == []
        live = rig.stops(position.id, *LIVE)
        if not refused:
            [gtt] = live
            assert gtt.purpose == STOP_GTT
            assert gtt.id != old.id
            assert D(gtt.trigger_price) == broker_trigger(D(position.current_stop), TICK)
            assert gtt.qty == position.qty_open
            assert report.done == 1
            return
        assert live == []
        assert ALERT_STOP_NOT_PLACED in new_alerts
        rig.sync(_on(CHECK_DAY, 9, 16))  # the session is open: the fill sync protects it
        [day_stop] = rig.stops(position.id, *LIVE)
        assert day_stop.purpose == STOP_DAY
        assert D(day_stop.trigger_price) == broker_trigger(D(position.current_stop), TICK)


@settings(max_examples=12, deadline=None)
@given(
    shape=st.sampled_from(["cancelled", "dropped"]),
    other=st.sampled_from(["protect", "verify"]),  # the fill sync's step, or the nightly verify
)
@example(shape="cancelled", other="protect")
def test_ex_date_check_serialised(shape: str, other: str) -> None:
    """HAT-EXEC-145 (PR 47, CodeAnt): the 08:40 check runs on the job's thread while the fill
    sync runs on its own over the same desk. When the other path reaches the position between
    the check ending the broker-cancelled GTT and placing its new one, it waits on the
    position's stop lock and then finds the new GTT: exactly one GTT is placed. Harm: two GTT
    sales rest over the same shares, and the second trigger sells shares HAT no longer holds."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        session.add(
            HatCorporateAction(
                instrument_id=position.instrument_id,
                ex_date=CHECK_DAY,
                action_type=TYPE_DIVIDEND,
                adjustment_factor=D(1),
                raw_text="fixture",
                source="mcp",
            )
        )
        session.commit()
        _book_shape(rig, old, shape)
        now = _on(CHECK_DAY, 8, 40)
        desk = rig.desk
        tick_for = desk._tick_for  # noqa: SLF001
        threads: list[threading.Thread] = []

        def other_path() -> None:
            with Session(session.get_bind()) as second:
                if other == "protect":
                    desk.protect(second, position.id, now)
                else:
                    desk.verify(second, position.id, now)

        def tick_then_race(s: Session, p: HatPosition) -> Decimal | None:
            # the check's first tick read comes after the old GTT was ended, before the new one
            # is reserved: the other path starts there and gets a moment to run
            if not threads:
                threads.append(threading.Thread(target=other_path))
                threads[0].start()
                threads[0].join(timeout=0.5)
            return tick_for(s, p)

        desk._tick_for = tick_then_race  # type: ignore[method-assign]  # noqa: SLF001
        start = len(rig.fake.calls)
        rig.manager.check_ex_date_gtts(session, rig.account_id, now)
        assert threads, "the check never placed a stop"
        threads[0].join(timeout=10)
        assert not threads[0].is_alive()
        placed = [name for name, _ in rig.fake.calls[start:] if name == "place_gtt"]
        assert len(placed) == 1
        session.expire_all()
        assert len([row for row in rig.stops(position.id, *LIVE) if row.purpose == STOP_GTT]) == 1


class _Book:
    """Exit levels for any instrument: a view is always there to give."""

    def day(self, _day: date) -> _Book:
        return self

    def exit_view(self, *_args: object) -> object:
        return object()


@settings(max_examples=30, deadline=None)
@given(
    successor=st.sampled_from(["none", "mapped", "unmapped", "other_isin"]),
    shape=st.sampled_from(["resting", "cancelled", "dropped"]),
    renamed=st.booleans(),  # the new series also took a new NSE symbol
    cancel_ok=st.booleans(),
    morning=st.booleans(),  # the 08:40 check runs (its switch on), else the nightly verify alone
)
@example(successor="mapped", shape="cancelled", renamed=False, cancel_ok=True, morning=True)
@example(successor="mapped", shape="cancelled", renamed=True, cancel_ok=True, morning=True)
@example(successor="other_isin", shape="cancelled", renamed=False, cancel_ok=True, morning=True)
@example(successor="mapped", shape="resting", renamed=False, cancel_ok=True, morning=True)
@example(successor="mapped", shape="resting", renamed=False, cancel_ok=False, morning=True)
@example(successor="mapped", shape="resting", renamed=False, cancel_ok=True, morning=False)
def test_series_change_remapped(
    successor: str, shape: str, renamed: bool, cancel_ok: bool, morning: bool
) -> None:
    """HAT-EXEC-146 (PR 47, research G17): when the instrument of an open real position leaves
    the listing and the same ISIN trades on under another series (EQ to BE), the position is
    manage-only (no exit view), alerted URGENT `position.series_changed` naming the new series
    and its Fyers symbol (or `unmapped`) by the morning check and every nightly verify, and its
    GTT is checked against the book: one the broker cancelled is placed again under the
    successor's Fyers symbol, under its NSE symbol when it took a new one; one still listed
    under the old symbol (which takes no order) is cancelled first, and a refused cancel is
    alerted `gtt.unverified` with nothing placed and the run failed. The nightly verify does the
    same for a GTT still under the old symbol (a refused cancel leaves it refreshed). A delisted
    row with
    no such successor, or none delisted, is no
    series change. Harm: the old `-EQ` GTT is gone, every order under it is refused, and exit
    rules run on prices that stopped."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        rig.broker._instruments = lambda: fyers_instruments(session)  # noqa: SLF001
        old_instrument = session.get(HatInstrument, rig.world.instruments[0])
        assert old_instrument is not None
        session.add(
            HatBrokerSymbol(
                instrument_id=old_instrument.id, broker="fyers", broker_symbol=SYMBOLS["SBIN"]
            )
        )
        session.commit()
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        be_symbol = "NSE:SBINBE-BE" if renamed else "NSE:SBIN-BE"
        # the old GTT went out two days before the refresh that delisted the old row
        old.placed_at = rig.wall() - timedelta(days=2)
        if successor != "none":
            old_instrument.active = False
            old_instrument.delisted_on = trade_date_of(rig.wall()) - timedelta(days=1)
            moved = HatInstrument(
                market_id=old_instrument.market_id,
                isin="INE999999999" if successor == "other_isin" else old_instrument.isin,
                series="BE",
                symbol="SBINBE" if renamed else "SBIN",
                name="SBIN",
                face_value=D(1),
                active=True,
            )
            session.add(moved)
            session.flush()
            if successor == "mapped":
                session.add(
                    HatBrokerSymbol(instrument_id=moved.id, broker="fyers", broker_symbol=be_symbol)
                )
            session.commit()
        _book_shape(rig, old, shape)
        if not cancel_ok:
            rig.fake.script("cancel_gtt", NOT_FOUND)
        changed = successor in ("mapped", "unmapped")
        assert ALERT_SERIES_CHANGED in URGENT_ALERTS
        if morning:
            _check_series_morning(rig, position, old, successor, shape, cancel_ok, be_symbol)
        views = ExitViews(
            _Book(),  # type: ignore[arg-type]
            lambda _s, _iid: D(100),
            FixedClock(_on(CHECK_DAY, 9, 16)),
            lambda: CALENDAR,
        )
        session.refresh(position)
        assert (views(session, position) is None) == changed
        start, alerts = len(rig.fake.calls), len(rig.notifier.alerts)
        rig.manager.verify_stops(session, rig.account_id, _on(CHECK_DAY, 23, 0))
        nightly = [c for c, _, _ in rig.notifier.alerts[alerts:] if c == ALERT_SERIES_CHANGED]
        assert len(nightly) == (1 if changed else 0)
        if morning and successor == "mapped" and (shape != "resting" or cancel_ok):
            # the GTT the morning placed under the new symbol is left resting
            assert "place_gtt" not in [name for name, _ in rig.fake.calls[start:]]
        if successor == "unmapped":
            assert "place_gtt" not in [name for name, _ in rig.fake.calls[start:]]
        elif not morning and changed and shape == "resting":
            placed = [args for name, args in rig.fake.calls[start:] if name == "place_gtt"]
            names = [name for name, _ in rig.fake.calls[start:]]
            if not cancel_ok:
                assert placed == []  # refreshed in place; the next night tries again
            else:
                assert names.index("cancel_gtt") < names.index("place_gtt")
                [gtt] = placed
                if successor == "mapped":
                    assert gtt["symbol"] == be_symbol  # type: ignore[index]


def _alert_codes(rig: ManageRig, since: int) -> list[tuple[str, object]]:
    """(alert, its payload's code) of every alert after the first `since`."""
    return [(c, p.get("code")) for c, _, p in rig.notifier.alerts[since:]]


def _check_series_morning(
    rig: ManageRig,
    position: HatPosition,
    old: HatOrder,
    successor: str,
    shape: str,
    cancel_ok: bool,
    be_symbol: str,
) -> None:
    """The 08:40 check of `test_series_change_remapped`."""
    session = rig.session
    changed = successor in ("mapped", "unmapped")
    start, alerts = len(rig.fake.calls), len(rig.notifier.alerts)
    report = rig.manager.check_ex_date_gtts(session, rig.account_id, _on(CHECK_DAY, 8, 40))
    flagged = [p for c, _, p in rig.notifier.alerts[alerts:] if c == ALERT_SERIES_CHANGED]
    if not changed:
        assert flagged == []
        assert not _read_book(rig, start)
        assert _writes_since(rig, start) == []
    else:
        [payload] = flagged
        assert (payload["from"], payload["to"]) == ("EQ", "BE")
        assert payload["broker_symbol"] == (be_symbol if successor == "mapped" else "unmapped")
        placed = [args for name, args in rig.fake.calls[start:] if name == "place_gtt"]
        names = [name for name, _ in rig.fake.calls[start:]]
        if successor == "unmapped":
            # no Fyers symbol for BE: nothing goes out under the old `-EQ` one, and a GTT still
            # listed is left for the owner
            assert placed == []
            assert "cancel_gtt" not in names
            if shape != "resting":
                assert (ALERT_STOP_NOT_PLACED, CODE_SERIES_UNMAPPED) in _alert_codes(rig, alerts)
        elif shape == "resting" and not cancel_ok:
            assert placed == []
            assert ALERT_GTT_UNVERIFIED in rig.codes()
            assert report.failed == [position.id]
        else:
            if shape == "resting":
                assert names.index("cancel_gtt") < names.index("place_gtt")
            session.refresh(old)
            assert old.status == "cancelled"
            [gtt] = placed
            if successor == "mapped":
                assert gtt["symbol"] == be_symbol  # type: ignore[index]


@settings(max_examples=30, deadline=None)
@given(
    age=st.integers(0, 400),  # days since the GTT was placed
    cancel_ok=st.booleans(),
    raised=st.decimals(min_value=0, max_value=2, places=2),
)
@example(age=300, cancel_ok=True, raised=D(0))
@example(age=299, cancel_ok=True, raised=D(0))
@example(age=365, cancel_ok=False, raised=D("0.50"))
def test_verify_renews_old_gtt(age: int, cancel_ok: bool, raised: Decimal) -> None:
    """HAT-EXEC-063 (PR 47, research G24): the nightly verify cancels a GTT placed 300 days ago
    or more and places it again at the current stop (cancel first: the market is shut, and two
    GTTs must never rest together), with no `gtt.missing` alarm; a younger GTT, or one whose
    cancel the broker refuses, is refreshed in place as before and tried again the next night.
    Harm: Fyers expires a GTT a year after it was placed and the position is left without a
    stop (a modify may not reset that clock)."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        old.placed_at = rig.wall() - timedelta(days=age)
        position.current_stop = D(position.current_stop) + raised
        session.commit()
        resting = D(old.trigger_price)
        if not cancel_ok:
            rig.fake.script("cancel_gtt", NOT_FOUND)
        start, alerts = len(rig.fake.calls), len(rig.codes())
        rig.manager.verify_stops(session, rig.account_id, _on(CHECK_DAY, 23, 0))
        goal = broker_trigger(D(position.current_stop), TICK)
        [gtt] = rig.stops(position.id)
        names = [name for name, _ in rig.fake.calls[start:]]
        assert ALERT_GTT_MISSING not in rig.codes()[alerts:]
        if age >= 300 and cancel_ok:
            session.refresh(old)
            assert old.status == "cancelled"
            assert gtt.id != old.id
            assert D(gtt.trigger_price) == goal
            assert names.index("cancel_gtt") < names.index("place_gtt")
            assert "modify_gtt" not in names
        else:
            assert gtt.id == old.id
            assert D(gtt.trigger_price) == max(goal, resting)
            assert "place_gtt" not in names
