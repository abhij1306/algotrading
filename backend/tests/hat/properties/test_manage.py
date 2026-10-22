"""PBT manage.* (HAT-EXEC-060..064, HAT-DEC-B17, HAT-RISK-070, 06 §6): broker stops and exits of
real positions (`app.hat.execution.protect`, `fills`, `manage`) on SQLite, through the real Fyers
adapter over `FakeBroker`. Nothing reaches a real broker."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from hat.fakes.broker import accepted, rejected, unconfirmed
from hat_engine_support import exit_stack
from hat_exec_support import SYMBOLS
from hat_manage_support import TICK, ManageRig, at
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine import exits, lifecycle, trades
from app.hat.execution.broker import BrokerUnavailableError, InvalidIntentError
from app.hat.execution.manage import ALERT_EXIT_NOT_PLACED
from app.hat.execution.protect import (
    ALERT_GTT_MISSING,
    ALERT_GTT_SWAP_FAILED,
    ALERT_GTT_UNFILLED,
    ALERT_GTT_UNVERIFIED,
    ALERT_STOP_NOT_MOVED,
    ALERT_STOP_NOT_PLACED,
    LIVE,
    STOP_DAY,
    STOP_GTT,
    broker_trigger,
)
from app.hat.models import HatEvent, HatOrder, HatPosition, HatSignal
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


@settings(max_examples=30, deadline=None)
@given(
    reply=st.sampled_from(["ok", "not_found", "stuck", "unavailable", "unread"]),
    raised=st.decimals(min_value=0, max_value=3, places=2),
)
def test_verify_covers(reply: str, raised: Decimal) -> None:
    """HAT-EXEC-063, HAT-OPS-024: each night every open real position's GTT is refreshed to the
    current stop (`gtt.refreshed`). A GTT whose refresh the broker refuses is cancelled, alerted
    (`gtt.missing`) and placed again; when the broker cannot be reached, the refused GTT cannot
    be cancelled or the fills cannot be read, the position is alerted (`gtt.unverified`) and no
    GTT is modified or placed, since the old GTT may still rest there and a second one would
    sell the shares twice. Harm: a GTT that triggered, expired or was never placed goes
    unnoticed and the position opens the next day without a stop."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [old] = rig.stops(position.id)
        if raised > 0:
            position.current_stop = D(position.current_stop) + raised
            session.commit()
        if reply != "ok":
            rig.fake.script("modify_gtt", UNAVAILABLE if reply == "unavailable" else NOT_FOUND)
        if reply == "stuck":
            rig.fake.script("cancel_gtt", NOT_FOUND)
        if reply == "unread":  # the trade book cannot be read: the open quantity may be stale
            rig.fake.script("fetch_tradebook", BrokerUnavailableError("Fyers tradebook failed"))
        places = rig.fake.count("place_gtt")
        rig.manager.verify_stops(session, rig.account_id, at(23, 0))
        goal = broker_trigger(D(position.current_stop), TICK)
        stops = rig.stops(position.id)
        if reply in ("unavailable", "stuck", "unread"):
            assert ALERT_GTT_UNVERIFIED in rig.codes()
            assert rig.fake.count("place_gtt") == places
            assert reply != "unread" or rig.fake.count("modify_gtt") == 0
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
            sales[variant] = [(o["type"], o["quantity"]) for o in orders]
    assert sales[kill] == sales["none"]


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
        assert trades.replay_state(session, signal.id) == state
        assert rig.position(outcome.signal_id) is None
        assert _writes_since(rig, start) == []


# ---------------------------------------------------------------------------------------
# HAT-EXEC-062: a GTT that triggers and fills only in part
# ---------------------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(part=st.floats(min_value=0.1, max_value=0.9), ended=st.booleans())
def test_gtt_trigger_handled(part: float, ended: bool) -> None:
    """HAT-EXEC-062, HAT-DEC-B17: a GTT that triggered and filled only in part is not resized
    or doubled while its sale may still sell the rest; once that sale ends short, `gtt.unfilled`
    is raised and the shares left get a new stop for exactly their quantity. Harm: a second
    stop under a sale still working sells the shares twice, or the rest stays unprotected."""
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        position = _opened(rig, at(16, 0))
        [gtt] = rig.stops(position.id)
        sold = max(1, int(gtt.qty * part))
        order = {
            "id": "X-1",
            "orderTag": None,
            "symbol": SYMBOLS["SBIN"],
            "side": -1,
            "qty": gtt.qty,
            "filledQty": sold,
            "status": 6,
            "productType": "CNC",
            "type": 1,
        }
        rig.fake.orders = [order]
        rig.fake.tradebook = [
            *rig.fake.tradebook,  # type: ignore[misc]
            {
                "tradeNumber": "T-G1",
                "orderNumber": "X-1",
                "orderTag": None,
                "symbol": SYMBOLS["SBIN"],
                "side": -1,
                "tradedQty": sold,
                "tradePrice": float(D(gtt.trigger_price) - 1),
            },
        ]
        start = len(rig.fake.calls)
        rig.sync(at(10, 0) + timedelta(days=1))
        session.refresh(position)
        assert position.qty_open == gtt.qty - sold
        assert _writes_since(rig, start) == []
        if not ended:
            return
        order["status"] = 1  # the rest of the sale was cancelled
        rig.sync(at(10, 5) + timedelta(days=1))
        assert rig.codes().count(ALERT_GTT_UNFILLED) == 1
        [stop] = [row for row in rig.stops(position.id) if row.id != gtt.id]
        assert stop.qty == position.qty_open
        rig.sync(at(10, 10) + timedelta(days=1))
        assert rig.codes().count(ALERT_GTT_UNFILLED) == 1


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
def test_async_sale_rejection_retried(ending: str, part: int, again: bool) -> None:
    """HAT-EXEC-075, HAT-OPS-032: a sale the broker accepted and then ended short (rejected by
    the exchange or RMS, cancelled, expired) records `order.rejected` (relayed as the health
    alert), fires its rule again and is sent once more under the next tag for what is still
    open: the full exit stays a full exit, never lost to a later partial. A second such ending
    raises `exit.not_placed` and nothing more is sent that session; the position is protected
    again each time. Harm: a sale the broker drops leaves the position unsold with its rule
    spent, silently. (Orders are stamped on the rig's trade day, so the session's rejections
    are counted on it.)"""
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", lambda: at(11, 0)),
    ):
        rig = ManageRig(session, exits=exit_stack("partials"))
        position = _opened(rig, at(10, 40))
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
        [row] = end_sales(1)
        session.refresh(position)
        assert _event_count(session, row, "order.rejected") == 1
        assert _resting(rig, position)
        start = len(rig.fake.calls)
        rig.manager.on_price(session, position.id, view, at(11, 2))
        retry = _sold(rig, start)
        assert [o["quantity"] for o in retry] == [position.qty_open]
        assert retry[0]["order_tag"] != first[0]["order_tag"]
        if again:
            end_sales(3)
            assert ALERT_EXIT_NOT_PLACED in rig.codes()
            start = len(rig.fake.calls)
            rig.manager.on_price(session, position.id, view, at(11, 4))
            assert _sold(rig, start) == []
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
