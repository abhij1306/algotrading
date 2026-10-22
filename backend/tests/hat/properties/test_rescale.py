"""PBT rescale.* (HAT-EXEC-140..143, audit F3): an open position spanning a split or bonus is
rescaled before the ex-date open (quantity ⌊q / f⌋, prices × f, once per action, with its event),
no exit fires from the action alone, the instrument is manage-only until the rescale is done, the
broker stop follows it down, and the night before looks the next session's actions up for the held
and watchlisted instruments. Real engine, SQLite in memory, `FakeBroker`; no network."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

import pytest
from hat_engine_support import exit_stack, open_trade, seed_engine_world
from hat_manage_support import ManageRig, at
from hat_mcp_support import ScriptedTransport
from hat_risk_support import CALENDAR, TRADE_DAY, valid_ctx, version_of
from hat_support import memory_engine
from hat_trigger_support import candidate, paise, playbook
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.clock import FixedClock
from app.hat.daily.live import ExitViews
from app.hat.daily.nightly import Nightly
from app.hat.data.corporate_actions import TYPE_BONUS, TYPE_OTHER, TYPE_SPLIT, factor_to_decimal
from app.hat.data.mcp_client import McpClient, McpTransportError
from app.hat.engine import exits, rescale, trades
from app.hat.engine.criteria.live import LiveContext
from app.hat.engine.live_entry import submit_trigger
from app.hat.engine.trigger import DailyInputs, Trigger, TriggerEvaluation, plan_entry
from app.hat.execution.protect import LIVE, STOP_DAY, broker_trigger
from app.hat.models import (
    HatCorporateAction,
    HatEvent,
    HatInstrument,
    HatMarket,
    HatPosition,
    HatSignal,
    HatTradingCalendar,
    HatWatchlistEntry,
)
from app.hat.notify import RecordingNotifier
from app.hat.risk.policy import seed_policy
from app.hat.time import IST, as_utc

D = Decimal
EX_DAY = TRADE_DAY + timedelta(days=1)  # Thursday: the ex-date after an entry on Wednesday
MORNING = datetime.combine(EX_DAY, time(8, 40), tzinfo=IST)
OPEN = datetime.combine(EX_DAY, time(9, 16), tzinfo=IST)
# Split a -> b gives b / a; a bonus of x for y held gives y / (x + y) (02 §5, HAT-DATA-055).
RATIOS = st.sampled_from(
    [
        (TYPE_SPLIT, Fraction(1, 2)),
        (TYPE_SPLIT, Fraction(1, 5)),
        (TYPE_SPLIT, Fraction(1, 10)),
        (TYPE_SPLIT, Fraction(2, 5)),
        (TYPE_BONUS, Fraction(1, 2)),
        (TYPE_BONUS, Fraction(2, 3)),
        (TYPE_BONUS, Fraction(2, 5)),
        (TYPE_BONUS, Fraction(10, 11)),
        (TYPE_BONUS, Fraction(4, 5)),
    ]
)
STACKS = st.sampled_from(["trend", "partials", "stop_only"])


def _action(session: Session, instrument: uuid.UUID, ex: date, kind: str, f: Fraction) -> None:
    session.add(
        HatCorporateAction(
            instrument_id=instrument,
            ex_date=ex,
            action_type=kind,
            adjustment_factor=factor_to_decimal(f),
            raw_text="fixture",
            source="mcp",
        )
    )
    session.commit()


def _position(session: Session, position_id: uuid.UUID) -> HatPosition:
    row = session.get(HatPosition, position_id)
    assert row is not None
    session.refresh(row)
    return row


def _r_distance(p: HatPosition) -> Fraction:
    entry, initial, current = (
        Fraction(D(v)) for v in (p.avg_entry, p.initial_stop, p.current_stop)
    )
    return (current - initial) / (entry - initial)


# --- HAT-EXEC-141: the rescale keeps the position -----------------------------------------------


def _decision(result: exits.Evaluation) -> tuple[list[tuple[str, tuple[str, ...]]], list[str]]:
    """What an evaluation does, without its quantities and prices: which rules sell and fire."""
    return sorted((a.kind, a.rule_ids) for a in result.actions), sorted(result.fired)


def _scaled(value: Decimal, f: Fraction) -> Decimal:
    return (value * D(f.numerator) / D(f.denominator)).quantize(D("0.0001"))


@settings(max_examples=80, deadline=None)
@given(
    ratio=RATIOS,
    stack=STACKS,
    raised=st.integers(0, 499),  # the stop moved up by this many paise before the action
    close=st.integers(-200, 2_000),  # the last close this many paise above the stop
    sma=st.integers(-1_000, 1_000),  # SMA10 this many paise above the close
    phase=st.sampled_from(["live", "close"]),
)
@example(
    ratio=(TYPE_SPLIT, Fraction(1, 2)), stack="stop_only", raised=0, close=500, sma=0, phase="live"
)
def test_preserves_position(
    ratio: tuple[str, Fraction], stack: str, raised: int, close: int, sma: int, phase: str
) -> None:
    """For every split or bonus ratio and position (entry 100, stop 95 raised by up to 4.99), the
    pre-open rescale gives quantity ⌊q / f⌋ and prices × f, keeping quantity × entry within one
    share, open risk within rounding and the stop's distance in R; and on the ex-date at the old
    price × f (SMA10 scaled the same) the exit stack decides exactly what it decided for the old
    position at the old price: no exit fires from the action alone (a price exactly on a rule's
    threshold is skipped, since rounding to 4 places may move it across). Harm: the ex-date open
    sells the position at a fake `hard_stop` or books a fake 50% loss (F3)."""
    kind, f = ratio
    engine = memory_engine()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        world = seed_engine_world(session, exits=exit_stack(stack), instruments=1)
        trade = open_trade(session, world)
        assert trade.outcome.position_id is not None
        before = _position(session, trade.outcome.position_id)
        before.current_stop = D(before.current_stop) + paise(raised)
        session.commit()
        qty, entry = before.qty_open, Fraction(D(before.avg_entry))
        risk = (entry - Fraction(D(before.initial_stop))) * qty
        distance = _r_distance(before)
        stops = (D(before.initial_stop), D(before.current_stop))
        last = D(before.current_stop) + paise(close)
        level = last + paise(sma)
        rules, state = trades.position_state(session, before)

        def old_at(price: Decimal) -> tuple[list, list[str]]:
            view = exits.ExitView(price=price, days_held=1, sma={10: level})
            return _decision(exits.evaluate(rules, state, view, phase))  # type: ignore[arg-type]

        expected = old_at(last)
        assume(old_at(last - paise(1)) == expected == old_at(last + paise(1)))
        old_view = exits.ExitView(price=last, days_held=1, sma={10: level})
        old_sold = [a.qty for a in exits.evaluate(rules, state, old_view, phase).actions]  # type: ignore[arg-type]
        _action(session, world.instruments[0], EX_DAY, kind, f)
        applied, failed = rescale.rescale_open_positions(
            factory, EX_DAY, MORNING, RecordingNotifier()
        )
        assert (applied, list(failed)) == (1, [])
        after = _position(session, before.id)
        new_entry = Fraction(D(after.avg_entry))
        assert after.qty_open == int(Fraction(qty) / f)
        assert abs(new_entry - entry * f) <= Fraction(1, 20_000)  # half a stored place
        assert abs(after.qty_open * new_entry - qty * entry) <= new_entry + qty * Fraction(1, 10**4)
        new_risk = (new_entry - Fraction(D(after.initial_stop))) * after.qty_open
        assert abs(new_risk - risk * after.qty_open * f / qty) <= after.qty_open * Fraction(
            2, 10**4
        )
        assert abs(_r_distance(after) - distance) <= Fraction(1, 1_000)
        for was, now in zip(stops, (after.initial_stop, after.current_stop), strict=True):
            exact = Fraction(was) * f  # a stop rounds down, never above the exact stop
            assert exact - Fraction(1, 10**4) < Fraction(D(now)) <= exact
        assert D(after.current_stop) >= D(after.initial_stop)
        view = exits.ExitView(price=_scaled(last, f), days_held=1, sma={10: _scaled(level, f)})
        result = trades.evaluate_position(session, after.id, view, phase, OPEN)  # type: ignore[arg-type]
        session.commit()
        assert _decision(result) == expected
        # The same sales, of the rescaled shares: a fraction of the scaled entry quantity.
        for sold, was in zip((a.qty for a in result.actions), old_sold, strict=True):
            assert abs(Fraction(sold) - Fraction(was) / f) <= 1 / f + 1, (sold, was)


# --- HAT-EXEC-141: each action once, never for a position opened on or after it -----------------


@settings(max_examples=40, deadline=None)
@given(
    first=RATIOS,
    second=RATIOS,
    early=st.integers(-3, 0),  # an ex-date on or before the entry day: never applied
    runs=st.lists(st.sampled_from(["run", "add_second", "add_early"]), min_size=1, max_size=6),
    noise=st.sampled_from(["none", "other", "consolidation"]),
)
def test_applied_once(
    first: tuple[str, Fraction],
    second: tuple[str, Fraction],
    early: int,
    runs: list[str],
    noise: str,
) -> None:
    """For every sequence of `ca.rescale` runs, with actions stored between them, each (position,
    action) is applied exactly once, in ex-date order; an action whose ex-date is on or before
    the day the position opened is never applied, nor is an `other` action (a rights issue) or
    a consolidation (factor above 1) on the ex-date. Harm: a split applied twice halves the
    prices again, a position bought after the split is halved, or a rights issue adds shares
    the account does not hold."""
    engine = memory_engine()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        world = seed_engine_world(session, instruments=1)
        trade = open_trade(session, world)
        position_id = trade.outcome.position_id
        assert position_id is not None
        start = _position(session, position_id)
        qty, entry = start.qty_open, Fraction(D(start.avg_entry))
        iid = world.instruments[0]
        _action(session, iid, EX_DAY, first[0], first[1])
        stored = [first[1]]
        if noise == "other":
            _action(session, iid, EX_DAY, TYPE_OTHER, Fraction(19, 20))
        elif noise == "consolidation":
            merged = TYPE_BONUS if first[0] == TYPE_SPLIT else TYPE_SPLIT
            _action(session, iid, EX_DAY, merged, Fraction(2))
        later = EX_DAY + timedelta(days=1)
        for step in runs:
            if step == "run":
                rescale.rescale_open_positions(factory, later, MORNING, RecordingNotifier())
            elif step == "add_second" and len(stored) == 1 and second[0] != first[0]:
                _action(session, iid, later, second[0], second[1])
                stored.append(second[1])
            elif step == "add_early" and not session.scalar(
                select(HatCorporateAction).where(HatCorporateAction.ex_date <= TRADE_DAY)
            ):
                _action(session, iid, TRADE_DAY + timedelta(days=early), TYPE_SPLIT, Fraction(1, 2))
        rescale.rescale_open_positions(factory, later, MORNING, RecordingNotifier())
        rescale.rescale_open_positions(factory, later, MORNING, RecordingNotifier())
        events = list(
            session.scalars(
                select(HatEvent.payload).where(
                    HatEvent.aggregate_id == position_id,
                    HatEvent.event_type == rescale.EVENT_RESCALED,
                )
            )
        )
        keys = [p["action"] for p in events]
        assert len(keys) == len(set(keys)) == len(stored)
        assert all(date.fromisoformat(k.split(":")[0]) > TRADE_DAY for k in keys)
        end = _position(session, position_id)
        want_qty = qty
        for f in stored:
            want_qty = int(Fraction(want_qty) / f)
        assert end.qty_open == want_qty
        product = Fraction(1)
        for f in stored:
            product *= f
        assert abs(Fraction(D(end.avg_entry)) - entry * product) <= Fraction(len(stored), 10**4)
        assert rescale.pending_actions(session, end, later) == []


@settings(max_examples=200, deadline=None)
@given(
    d=st.integers(2, rescale.MAX_DENOMINATOR),
    n=st.integers(1, rescale.MAX_DENOMINATOR - 1),
    places=st.integers(2, 5),  # a factor sent short; a non-exact one is then 1e-8 or more off
)
def test_factor_exact(d: int, n: int, places: int) -> None:
    """For every ratio n/d below 1 with d up to 1000, the stored 12-place factor gives back n/d
    exactly; and a factor sent to fewer places that is not exactly such a ratio is refused (the
    position stays manage-only) rather than read as some nearby ratio. Harm: 0.0110 read as
    11/1000 instead of 1/91 sizes the rescaled position at a tenth of its shares."""
    assume(n < d)
    f = Fraction(n, d)
    assert rescale.exact_factor(factor_to_decimal(f)) == f
    short = (D(n) / D(d)).quantize(D(1).scaleb(-places))
    nearest = Fraction(short).limit_denominator(rescale.MAX_DENOMINATOR)
    if nearest != Fraction(short):
        stored = factor_to_decimal(Fraction(short))
        with pytest.raises(ValueError):
            rescale.exact_factor(stored)


# --- HAT-EXEC-142: until rescaled, no exit and no entry ---------------------------------------


class _Book:
    """The daily levels of the exits: a fixed view for any instrument."""

    def day(self, _day: date) -> _Book:
        return self

    def exit_view(
        self, _iid: uuid.UUID, price: Decimal, _rules: object, held: int
    ) -> exits.ExitView:
        return exits.ExitView(price=price, days_held=held, sma={10: price})


@settings(max_examples=40, deadline=None)
@given(ratio=RATIOS, pending=st.booleans(), price=st.integers(1_000, 20_000))
def test_pending_is_manage_only(ratio: tuple[str, Fraction], pending: bool, price: int) -> None:
    """While an open position has a stored action it was not rescaled for, the exits' view source
    gives it no view at any price and a trigger of its instrument is stored Rejected with
    `corporate_action_pending`; once rescaled (or with no action), the view returns and the
    trigger goes to the risk authority. Harm: exits or entries decided on prices of the wrong
    scale."""
    kind, f = ratio
    engine = memory_engine()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        world = seed_engine_world(session, instruments=1)
        trade = open_trade(session, world)
        position = _position(session, trade.outcome.position_id)  # type: ignore[arg-type]
        iid = world.instruments[0]
        _action(session, iid, EX_DAY, kind, f)
        if not pending:
            rescale.rescale_open_positions(factory, EX_DAY, MORNING, RecordingNotifier())
        views = ExitViews(
            _Book(),  # type: ignore[arg-type]
            lambda _iid: paise(price),
            FixedClock(OPEN),
            lambda: CALENDAR,
        )
        view = views(session, _position(session, position.id))
        assert (view is None) == pending
        assert rescale.awaits_rescale(session, iid, EX_DAY) == pending
        book = playbook(stop={"rule": "pct_below_entry", "pct": 5.0})
        daily = DailyInputs(
            prev_close=paise(price),
            adv20=D(10**9),
            adtr20=paise(price // 10),
            tick=D("0.05"),
            band_known=True,
            band_pct=None,
        )
        cand = candidate(
            book, daily=daily, bot_id=world.bot_id, instrument_id=iid, trade_date=EX_DAY
        )
        when = OPEN + timedelta(minutes=10)
        ctx = LiveContext(at=when, price=paise(price))
        plan = plan_entry(cand, ctx, paise(price))
        fired = Trigger(cand, when, TriggerEvaluation(True, (), paise(price)), plan, ctx)
        stored = replace(version_of(seed_policy()), version_id=world.policy_version_id)
        outcome = submit_trigger(session, fired, valid_ctx(policy_versions=[stored], at=when))
        signal = session.get(HatSignal, outcome.signal_id)
        assert signal is not None
        reason = (signal.risk_decision or {}).get("reason")
        assert (reason == rescale.SKIP_ACTION_PENDING) == pending, reason
        if pending:
            assert signal.state == "Rejected"
            assert outcome.position_id is None


# --- HAT-EXEC-143: the broker stop follows the rescale down ---------------------------------------


def _refuse_larger(rig: ManageRig, size: int) -> None:
    """The broker before the new shares reach the holdings: a stop for more than `size` is
    refused, anything else answered as usual."""
    fake = rig.fake
    for name in ("modify_order", "modify_gtt"):
        real = getattr(fake, name)

        def refusing(order_id: str, params: dict, real=real, name=name) -> object:  # noqa: ANN001
            if params.get("quantity", 0) > size:
                fake.calls.append((name, (order_id, params)))
                return {"s": "error", "message": "insufficient holdings"}
            return real(order_id, params)

        setattr(fake, name, refusing)


@settings(max_examples=25, deadline=None)
@given(ratio=RATIOS, gtt=st.booleans(), rescaled=st.booleans(), credited=st.booleans())
def test_broker_stop_follows(
    ratio: tuple[str, Fraction], gtt: bool, rescaled: bool, credited: bool
) -> None:
    """For every rescaled real position, after the next fill sync every live broker stop (a GTT,
    or a day stop) rests at the rescaled stop rounded down to the tick, for the rescaled
    quantity, and no modify on the way sized the stop up while its trigger was still above the
    rescaled stop; while the new shares are not yet credited (the broker refuses a larger stop)
    the stop still rests at the rescaled trigger for the old size; for a position never rescaled
    no stop moves. Harm: the old GTT at the pre-split
    trigger fires on the ex-date open and sells part of the position for nothing."""
    kind, f = ratio
    with Session(memory_engine()) as session:
        rig = ManageRig(session)
        outcome = rig.enter(at(10, 30))
        row = rig.entry_row(outcome.signal_id)
        assert row is not None
        rig.fill(row, row.qty, D(row.price))
        rig.sync(at(16, 0) if gtt else at(10, 40))
        position = rig.position(outcome.signal_id)
        assert position is not None
        [old] = rig.stops(position.id, *LIVE)
        resting, size = D(old.trigger_price), old.qty
        if rescaled:
            _action(session, rig.world.instruments[0], EX_DAY, kind, f)
            factory = sessionmaker(bind=session.get_bind())
            rescale.rescale_open_positions(factory, EX_DAY, MORNING, RecordingNotifier())
        moves = len(rig.fake.calls)
        if not credited:
            _refuse_larger(rig, size)
        rig.sync(at(16, 5) if gtt else at(10, 45))
        position = rig.position(outcome.signal_id)
        assert position is not None
        # No modify ever leaves more than the old size under a trigger above the rescaled stop: a
        # full-size stop at the old trigger sells the position at the open.
        goal = float(broker_trigger(D(position.current_stop), rig.tick))
        for call, args in rig.fake.calls[moves:]:
            if call in ("modify_order", "modify_gtt"):
                params = args[1]
                if params.get("quantity", 0) > size:
                    assert params["trigger_price"] <= goal, (call, params, goal)
        stops = rig.stops(position.id, *LIVE)
        assert stops, "the position lost its broker stop"
        for stop in stops:
            if rescaled:
                assert D(stop.trigger_price) == broker_trigger(D(position.current_stop), rig.tick)
                held = position.qty_open if credited else size
                assert stop.qty == held
                if stop.purpose == STOP_DAY:  # the broker's order was resized, not only the row
                    accepted = [
                        p
                        for _, p in rig.fake.calls_named("modify_order")
                        if credited or p.get("quantity", size) <= size
                    ]
                    assert accepted[-1].get("quantity", size) == held
                else:
                    [gtt] = [g for g in rig.fake.resting_gtts() if g["id"] == stop.broker_order_id]
                    assert gtt["qty"] == held
            else:
                assert (D(stop.trigger_price), stop.qty) == (resting, size)


# --- HAT-EXEC-140: the night before ---------------------------------------------------------------


def _seed_symbols(session: Session, ids: list[uuid.UUID]) -> dict[str, uuid.UUID]:
    market_id = session.scalar(select(HatMarket.id))
    symbols = {}
    for n, iid in enumerate(ids):
        symbol = f"SYM{n}"
        session.add(
            HatInstrument(
                id=iid,
                market_id=market_id,
                isin=f"INE00000000{n}",
                series="EQ",
                symbol=symbol,
                name=symbol,
                face_value=D(1),
                active=True,
            )
        )
        symbols[symbol] = iid
    for day, row in CALENDAR.days.items():
        session.add(
            HatTradingCalendar(
                market_id=market_id,
                day=day,
                is_trading=row.is_trading,
                open_at=as_utc(row.open_at) if row.open_at else None,
                close_at=as_utc(row.close_at) if row.close_at else None,
            )
        )
    session.commit()
    return symbols


@settings(max_examples=30, deadline=None)
@given(
    held=st.sets(st.integers(0, 5), max_size=3),
    watched=st.sets(st.integers(0, 5), max_size=3),
    actions=st.sets(st.integers(0, 5)),
    failing=st.sets(st.integers(0, 5)),
    malformed=st.sets(st.integers(0, 5)),
    configured=st.booleans(),
)
def test_next_session_lookup(
    held: set[int],
    watched: set[int],
    actions: set[int],
    failing: set[int],
    malformed: set[int],
    configured: bool,
) -> None:
    """For every set of held and watchlisted instruments and MCP answers, `ca.adjust` looks up
    exactly the held and watchlisted symbols, each over (next session, next session); stores
    every action returned; and alerts each failed lookup, or one with a row it cannot read,
    once while the step still succeeds;
    with no MCP client the step still succeeds (the features and scan run) and alerts once.
    Harm: a split of a held name is first seen after its ex-date close, when the fake stop-out
    has already happened."""
    engine = memory_engine()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        world = seed_engine_world(session, instruments=6)
        symbols = _seed_symbols(session, world.instruments)
        for n in sorted(held):
            open_trade(session, world, world.instruments[n], playbook=f"pb{n}")
        for n in sorted(watched):
            session.add(
                HatWatchlistEntry(
                    bot_id=world.bot_id,
                    trade_date=TRADE_DAY,
                    instrument_id=world.instruments[n],
                    strategy_version_id=world.version_id,
                    playbook="pb",
                    status="Watchlisted",
                    score=D(1),
                    criteria_results={},
                )
            )
        session.commit()
    target = CALENDAR.next_trading_day(TRADE_DAY)

    def respond(tool: str, arguments: dict) -> object:
        assert tool == "get_corporate_actions"
        n = int(arguments["symbol"][3:])
        if n in failing:
            return McpTransportError()
        rows = [
            {
                "exDate": target.isoformat(),
                "actionType": "SPLIT",
                "purpose": "Face Value Split",
                "adjustmentFactor": 0.5,
            }
        ]
        if n in malformed:  # a row with no factor: unreadable, so the answer is incomplete
            rows = [{"exDate": target.isoformat(), "actionType": "BONUS", "purpose": "Bonus"}]
        return {"symbol": arguments["symbol"], "actions": rows if n in actions | malformed else []}

    transport = ScriptedTransport(respond)
    notifier = RecordingNotifier()
    client = McpClient(transport, sleep=lambda _s: None)
    store = SimpleNamespace(connect=lambda _factors: contextlib.nullcontext())
    deps = SimpleNamespace(
        session_factory=factory,
        mcp=lambda: client if configured else None,
        notifier=notifier,
        store=store,
    )
    Nightly(deps).ca_adjust(TRADE_DAY)  # type: ignore[arg-type]
    wanted = held | watched
    if not configured:
        assert not transport.calls
        expected = [{"symbol": "*"}] if wanted else []
        assert [d for _, _, d in notifier.alerts] == expected
        return
    asked = [a for _, tool, a in transport.calls if tool == "get_corporate_actions"]
    # A failed lookup is retried by the client; every symbol is asked, and only those.
    assert {a["symbol"] for a in asked} == {f"SYM{n}" for n in wanted}
    assert all((a["fromDate"], a["toDate"]) == (target.isoformat(),) * 2 for a in asked)
    with factory() as session:
        stored = set(session.scalars(select(HatCorporateAction.instrument_id)))
        count = session.scalar(select(func.count()).select_from(HatCorporateAction))
    assert stored == {symbols[f"SYM{n}"] for n in wanted & actions - failing - malformed}
    assert count == len(stored)
    alerted = sorted(d["symbol"] for code, _, d in notifier.alerts)
    assert alerted == sorted(f"SYM{n}" for n in wanted & (failing | malformed))
    assert {code for code, _, _ in notifier.alerts} <= {rescale.ALERT_ACTION_UNCHECKED}
