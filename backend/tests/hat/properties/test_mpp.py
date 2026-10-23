"""PBT mpp.*: HAT's sales under SEBI market
price protection, which turns a market sale into a protected limit that can rest unfilled. The
real fill sync and trade manager on SQLite, through the real Fyers adapter over `FakeBroker`; the
order-book shape of a protected sale is the assumed fixture `fyers_mpp_sale_assumed.json` (the
O2(c) live check confirms it). Nothing reaches a real broker."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hat.fakes.broker import rejected
from hat_engine_support import exit_stack
from hat_exec_support import SYMBOLS
from hat_manage_support import TICK, ManageRig, at
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine import exits
from app.hat.execution.fills import ALERT_SALE_UNFILLED, LOWER_CIRCUIT_LOCKED, SALE_RESTING
from app.hat.execution.manage import ALERT_EXIT_NOT_PLACED, BAND_LIMIT
from app.hat.execution.submit import rejected_sales, resends
from app.hat.models import HatEvent, HatOrder, HatPosition
from app.hat.settings import load_settings
from app.hat.telegram.render import URGENT_ALERTS
from app.utils.env_loader import TRUE_VALUES

D = Decimal
SALES = ("exit_partial", "exit_full", "flatten")
SHAPES: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fyers_mpp_sale_assumed.json").read_text("utf-8")
)["shapes"]
PAGE_AFTER = 3
WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "cancel_gtt")


class _Band:
    """The day's lower band the trade manager and fill sync read (`fills.SaleBand`)."""

    def __init__(self, floor: Decimal | None = None, locked: bool | None = False) -> None:
        self.floor = floor
        self.is_locked = locked

    def lower(self, _session: Session, _iid: uuid.UUID, _now: datetime) -> Decimal | None:
        return self.floor

    def locked(self, _session: Session, _iid: uuid.UUID, _now: datetime) -> bool | None:
        return self.is_locked


def _opened(rig: ManageRig) -> HatPosition:
    """An entry routed at 10:30 and filled by the sync at 10:40."""
    outcome = rig.enter(at(10, 30))
    row = rig.entry_row(outcome.signal_id)
    assert row is not None
    rig.fill(row, row.qty, D(row.price))
    rig.sync(at(10, 40))
    position = rig.position(outcome.signal_id)
    assert position is not None
    return position


def _view(position: HatPosition) -> exits.ExitView:
    """A price at which the `partials` stack sells the whole position."""
    return exits.ExitView(price=D(position.avg_entry) * D("1.4"), days_held=1)


def _placed(rig: ManageRig, start: int, kind: str = "MARKET") -> list[dict[str, Any]]:
    return [a for n, a in rig.fake.calls[start:] if n == "place_order" and a["type"] == kind]


def _cancels(rig: ManageRig, start: int) -> list[object]:
    return [a for n, a in rig.fake.calls[start:] if n == "cancel_order"]


def _sales(rig: ManageRig, position: HatPosition) -> list[HatOrder]:
    rows = list(
        rig.session.scalars(
            select(HatOrder)
            .where(HatOrder.position_id == position.id, HatOrder.purpose.in_(SALES))
            .order_by(HatOrder.tag)
        )
    )
    for row in rows:
        rig.session.refresh(row)
    return rows


def _book(rig: ManageRig, row: HatOrder) -> dict[str, Any]:
    return next(b for b in rig.fake.book if b["orderTag"] == row.tag)


def _shaped(rig: ManageRig, row: HatOrder, shape: str, reference: Decimal) -> Decimal | None:
    """Show the resting market sale `row` in the book as `shape`; the limit price it shows."""
    spec = SHAPES[shape]
    book = _book(rig, row)
    book["type"] = spec["type"]
    if spec["limit_below_pct"] is None:
        book.pop("limitPrice", None)
        return None
    limit = (reference * (100 - D(spec["limit_below_pct"])) / 100 / TICK).to_integral() * TICK
    book["limitPrice"] = float(limit)
    return limit


def _alerts(rig: ManageRig, code: str) -> list[dict[str, str | int]]:
    return [d for n, _, d in rig.notifier.alerts if n == ALERT_SALE_UNFILLED and d["code"] == code]


def _events(session: Session, rows: list[HatOrder], kind: str) -> int:
    ids = [row.id for row in rows]
    return len(
        session.scalars(
            select(HatEvent.id).where(HatEvent.aggregate_id.in_(ids), HatEvent.event_type == kind)
        ).all()
    )


# ---------------------------------------------------------------------------------------
# step 3: a market sale resting unfilled is sent again fresh
# ---------------------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    shape=st.sampled_from(sorted(SHAPES)),
    misses=st.integers(min_value=1, max_value=4),
    parts=st.lists(st.integers(min_value=0, max_value=2), min_size=4, max_size=4),
    resend=st.booleans(),
)
@example(shape="market_protected", misses=3, parts=[0, 0, 0, 0], resend=True)
@example(shape="shown_as_limit", misses=2, parts=[1, 0, 2, 0], resend=True)
@example(shape="market_plain", misses=1, parts=[0, 0, 0, 0], resend=False)
def test_resting_market_sale_resent(
    shape: str, misses: int, parts: list[int], resend: bool
) -> None:
    """Under market price protection a market sale is a limit a band from the
    price, and rests unfilled once the price falls through it. The fill sync records the price
    the book shows for each sale on its row; with `resend` on, a market sale still resting a
    minute after its send is cancelled (`order.cancelled`, code `sale_resend`, with the book's
    type and price), and once the book shows it ended the shares it left go again as one fresh
    market sale, at every miss. A sale HAT cancelled is never a rejection: no `order.rejected`,
    no `exit.not_placed`, and `rejected_sales` stays 0, so the two-rejection limit never stops
    the re-sends. The owner is paged (URGENT `sale.unfilled`, code `sale_resting`) once, at the
    `page_after`th miss. With `resend` off nothing is cancelled and the sale is left resting.
    Harm: an exit or a stop-loss sale sits unfilled below a protection band all session while
    the price keeps falling, or a re-send sells the shares twice."""
    clock = [at(11, 0)]
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", side_effect=lambda: clock[0].astimezone(UTC)),
    ):
        rig = ManageRig(
            session,
            exits=exit_stack("partials"),
            band=_Band(),
            resend=resend,
            page_after=PAGE_AFTER,
        )
        position = _opened(rig)
        view = _view(position)
        start = len(rig.fake.calls)
        rig.manager.on_price(session, position.id, view, at(11, 0))
        assert [o["quantity"] for o in _placed(rig, start)] == [position.qty_open]
        minute = 0
        for miss in range(misses):
            row = _sales(rig, position)[-1]
            limit = _shaped(rig, row, shape, D(view.price))
            if parts[miss]:
                rig.fill(row, parts[miss], D(view.price))
            start = len(rig.fake.calls)
            rig.sync(clock[0] + timedelta(seconds=30))  # under a minute after its send
            assert _cancels(rig, start) == []
            minute += 1
            clock[0] = at(11, minute)
            start = len(rig.fake.calls)
            rig.sync(at(11, minute))  # a minute after its send: it rests
            session.refresh(row)
            if limit is not None:
                assert D(row.price) == limit  # the price the book shows, recorded
            if not resend:
                assert _cancels(rig, start) == []
                continue
            assert _cancels(rig, start) == [row.broker_order_id]
            assert resends(session, row.signal_id, row.purpose, at(11, minute)) == miss + 1
            [book] = session.scalars(
                select(HatEvent.payload).where(
                    HatEvent.aggregate_id == row.id, HatEvent.event_type == "order.cancelled"
                )
            ).all()
            assert book["book_order_type"] == {1: "LIMIT", 2: "MARKET"}[SHAPES[shape]["type"]]
            assert (book["book_price"] and D(book["book_price"])) == limit
            minute += 1
            clock[0] = at(11, minute)
            start = len(rig.fake.calls)
            rig.sync(at(11, minute))  # the book shows it cancelled: sent again, fresh
            session.refresh(position)
            again = _placed(rig, start)
            assert [o["quantity"] for o in again] == [position.qty_open]
            assert again[0]["order_tag"] != row.tag
        rows = _sales(rig, position)
        assert len(rows) == (misses + 1 if resend else 1)
        assert _events(session, rows, "order.rejected") == 0
        assert ALERT_EXIT_NOT_PLACED not in rig.codes()
        assert rejected_sales(session, rows[0].signal_id, rows[0].purpose, at(11, 0)) == 0
        assert len(_alerts(rig, SALE_RESTING)) == (1 if resend and misses >= PAGE_AFTER else 0)
        assert ALERT_SALE_UNFILLED in URGENT_ALERTS
        if not resend:
            assert _events(session, rows, "order.cancelled") == 0


# ---------------------------------------------------------------------------------------
# (G03 live): a sale at the lower circuit waits in the queue
# ---------------------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    locks=st.lists(st.sampled_from([True, False, None]), min_size=1, max_size=4),
    shape=st.sampled_from(sorted(SHAPES)),
)
@example(locks=[True, True, False], shape="market_protected")
@example(locks=[None], shape="market_plain")
def test_lower_circuit_lock_holds_the_sale(locks: list[bool | None], shape: str) -> None:
    """While the instrument's last trade is at its lower circuit (the stored
    band) a resting market sale is not cancelled: a re-send would lose its place in the
    sellers' queue, and nothing trades below the band. It is alerted once (`sale.unfilled`, code
    `lower_circuit_locked`) however many syncs it stays locked; at the first sync where the lock
    is gone (or not known) the sale is cancelled to go again. Harm: a locked sale is cancelled and
    re-sent every minute and never fills, or the owner is not told the position cannot be sold."""
    clock = [at(11, 0)]
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", side_effect=lambda: clock[0].astimezone(UTC)),
    ):
        band = _Band()
        rig = ManageRig(
            session, exits=exit_stack("partials"), band=band, resend=True, page_after=PAGE_AFTER
        )
        position = _opened(rig)
        view = _view(position)
        rig.manager.on_price(session, position.id, view, at(11, 0))
        [row] = _sales(rig, position)
        _shaped(rig, row, shape, D(view.price))
        cancelled_at = next((n for n, locked in enumerate(locks) if locked is not True), None)
        for n, locked in enumerate(locks):
            band.is_locked = locked
            clock[0] = at(11, n + 1)
            start = len(rig.fake.calls)
            rig.sync(at(11, n + 1))
            expected = [row.broker_order_id] if n == cancelled_at else []
            assert _cancels(rig, start) == expected
            if n == cancelled_at:
                break
        locked_first = cancelled_at is None or cancelled_at > 0
        assert len(_alerts(rig, LOWER_CIRCUIT_LOCKED)) == (1 if locked_first else 0)


@settings(max_examples=25, deadline=None)
@given(seconds=st.integers(min_value=0, max_value=150), shape=st.sampled_from(sorted(SHAPES)))
@example(seconds=59, shape="market_protected")
@example(seconds=60, shape="market_protected")
def test_no_cancel_without_a_resend(seconds: int, shape: str) -> None:
    """A resting market sale is cancelled only when its re-send, a sync (a
    minute) later, still falls before the 15:30 order cutoff; from then on it is left resting.
    Harm: a sale cancelled at 15:29:30 cannot be sent again, and the exit is lost for the
    session."""
    clock = [at(15, 27)]
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", side_effect=lambda: clock[0].astimezone(UTC)),
    ):
        rig = ManageRig(
            session, exits=exit_stack("partials"), band=_Band(), resend=True, page_after=PAGE_AFTER
        )
        position = _opened(rig)
        view = _view(position)
        rig.manager.on_price(session, position.id, view, at(15, 27))
        [row] = _sales(rig, position)
        _shaped(rig, row, shape, D(view.price))
        now = at(15, 28) + timedelta(seconds=seconds)
        clock[0] = now
        start = len(rig.fake.calls)
        rig.sync(now)
        allowed = now + timedelta(minutes=1) < at(15, 30)
        assert _cancels(rig, start) == ([row.broker_order_id] if allowed else [])


# ---------------------------------------------------------------------------------------
# (G22): a sale refused twice goes as a limit at the lower band
# ---------------------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    path=st.sampled_from(["at_once", "after_taking"]),
    floor=st.booleans(),
    last=st.sampled_from(["accepted", "rejected"]),
)
@example(path="at_once", floor=True, last="rejected")
@example(path="after_taking", floor=True, last="accepted")
@example(path="at_once", floor=False, last="accepted")
def test_rejected_twice_goes_at_the_band(path: str, floor: bool, last: str) -> None:
    """A sale refused twice in a session (by the broker at once, or rejected after
    the broker took it) is sent once more as a LIMIT SELL at the day's lower circuit price (the
    lowest price that can trade today, so no price check or protection band refuses it) for what
    is open, and the owner is told (`sale.unfilled`, code `band_limit`). Refused too, it raises
    `exit.not_placed` and nothing more is sent that session. Without a known band nothing goes
    after the second refusal, as before. Harm: a stop-loss sale refused twice leaves the position
    unsold all session while the price falls."""
    clock = [at(11, 0)]
    with (
        Session(memory_engine()) as session,
        patch("app.hat.execution.submit.utc_now", side_effect=lambda: clock[0].astimezone(UTC)),
    ):
        band = _Band()
        rig = ManageRig(session, exits=exit_stack("partials"), band=band, page_after=PAGE_AFTER)
        position = _opened(rig)
        view = _view(position)
        lower = (D(position.avg_entry) * D("0.8") / TICK).to_integral() * TICK
        band.floor = lower if floor else None
        refusal = rejected("RMS: price outside the protection band")
        if path == "at_once":
            rig.fake.script(
                "place_order", refusal, refusal, *([refusal] if last == "rejected" else [])
            )
            start = len(rig.fake.calls)
            rig.manager.on_price(session, position.id, view, at(11, 0))
        else:
            start = len(rig.fake.calls)
            for minute in (0, 2):
                clock[0] = at(11, minute)
                rig.manager.on_price(session, position.id, view, at(11, minute))
                row = _sales(rig, position)[-1]
                _book(rig, row)["status"] = 5  # the exchange rejected it after the broker took it
                rig.sync(at(11, minute + 1))
            if last == "rejected":
                rig.fake.script("place_order", refusal)
            clock[0] = at(11, 4)
            rig.manager.on_price(session, position.id, view, at(11, 4))
        session.refresh(position)
        limits = _placed(rig, start, "LIMIT")
        assert len(_placed(rig, start)) == 2  # the market sale and its one retry
        if not floor:
            assert limits == []
            assert ALERT_EXIT_NOT_PLACED in rig.codes()
            assert _alerts(rig, BAND_LIMIT) == []
            return
        assert [(o["side"], o["quantity"], D(str(o["price"]))) for o in limits] == [
            ("SELL", position.qty_open, lower)
        ]
        assert len(_alerts(rig, BAND_LIMIT)) == 1
        assert (ALERT_EXIT_NOT_PLACED in rig.codes()) == (last == "rejected" or path != "at_once")
        start = len(rig.fake.calls)
        clock[0] = at(11, 6)
        rig.manager.on_price(session, position.id, view, at(11, 6))
        assert _placed(rig, start) == []
        assert _placed(rig, start, "LIMIT") == []


# ---------------------------------------------------------------------------------------
# a triggered GTT's sale already at the market is asked again
# ---------------------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    syncs=st.integers(min_value=1, max_value=5),
    locked_from=st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
    resend=st.booleans(),
)
@example(syncs=4, locked_from=None, resend=True)
@example(syncs=3, locked_from=1, resend=True)
def test_gtt_market_sale_asked_again(syncs: int, locked_from: int | None, resend: bool) -> None:
    """PR 48 (G02), a triggered GTT's sale the book already shows as a market
    order but still resting (a protected limit the price left) is, with `resend` on, asked to go
    at the market again (exactly `{order_type: MARKET}` on its id) at every sync, so the broker
    sets the protection band at the price then; the owner is paged once at the `page_after`th
    miss. From the sync its instrument is locked at the lower circuit it is left in the queue and
    alerted once. With `resend` off a market sale is left alone, as before. No other order is
    sent. Harm: the triggered stop's sale sits below a stale protection band all session."""
    with Session(memory_engine()) as session:
        band = _Band()
        rig = ManageRig(session, band=band, resend=resend, page_after=PAGE_AFTER)
        position = _opened_overnight(rig)
        [gtt] = rig.stops(position.id)
        rig.fake.orders = [
            {
                "id": "X-1",
                "orderTag": gtt.tag,
                "symbol": SYMBOLS["SBIN"],
                "side": -1,
                "qty": gtt.qty,
                "filledQty": 0,
                "status": 6,
                "productType": "CNC",
                "type": 2,  # already a market sale, resting at its protection price
                "limitPrice": float(D(gtt.price) - 5),
            }
        ]
        day = timedelta(days=1)
        rig.sync(at(9, 15) + day)  # first sight: left a minute to fill
        start = len(rig.fake.calls)
        asked = 0
        for n in range(syncs):
            band.is_locked = locked_from is not None and n >= locked_from
            rig.sync(at(9, 16 + n) + day)
            asked += 1 if resend and not band.is_locked else 0
        writes = [(name, args) for name, args in rig.fake.calls[start:] if name in WRITES]
        assert writes == [("modify_order", ("X-1", {"order_type": "MARKET"}))] * asked
        assert len(_alerts(rig, SALE_RESTING)) == (1 if asked >= PAGE_AFTER else 0)
        locked_seen = resend and locked_from is not None and locked_from < syncs
        assert len(_alerts(rig, LOWER_CIRCUIT_LOCKED)) == (1 if locked_seen else 0)


def _opened_overnight(rig: ManageRig) -> HatPosition:
    """An entry filled by the 16:00 sync, so its stop is a GTT."""
    outcome = rig.enter(at(10, 30))
    row = rig.entry_row(outcome.signal_id)
    assert row is not None
    rig.fill(row, row.qty, D(row.price))
    rig.sync(at(16, 0))
    position = rig.position(outcome.signal_id)
    assert position is not None
    return position


@settings(max_examples=60, deadline=None)
@example(resend=None, page=None)
@example(resend="yes", page="0")
@given(
    resend=st.none() | st.sampled_from(["1", "true", " On ", "yes", "0", "off", "", "no", "maybe"]),
    page=st.none() | st.sampled_from(["1", "2", "5", "0", "-3", "x", ""]),
)
def test_resend_flag_parsed(resend: str | None, page: str | None) -> None:
    """: `HAT_MPP_RESEND` turns the re-send on exactly for a true value (off
    unset or for anything else), and `HAT_MPP_PAGE_AFTER` is a positive whole number, else 3.
    Harm: the re-send of an unconfirmed book shape is on by default, or a typo never pages."""
    env = {}
    if resend is not None:
        env["HAT_MPP_RESEND"] = resend
    if page is not None:
        env["HAT_MPP_PAGE_AFTER"] = page
    loaded = load_settings(env)
    assert loaded.mpp_resend == (resend is not None and resend.strip().lower() in TRUE_VALUES)
    number = int(page) if page is not None and page.lstrip("-").isdigit() else 0
    assert loaded.mpp_page_after == (number if number > 0 else 3)
