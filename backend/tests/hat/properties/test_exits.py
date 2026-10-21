"""PBT exits.* (HAT-EXEC-070..073, HAT-RISK-070, HAT-EXEC-042, HAT-EXEC-010): the exit stack of
`app.hat.engine.exits` against rules written out independently, and the shadow engine of
`app.hat.engine.trades` driven end to end on SQLite with a broker that records every call."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest
from hat_engine_support import (
    Trade,
    exit_stack,
    open_trade,
    seed_engine_world,
    step_trade,
)
from hat_support import memory_engine
from hypothesis import event, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine import exits, lifecycle, trades
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.strategy.config import (
    ExitBreakeven,
    ExitCloseBelowSma,
    ExitHardStop,
    ExitPartialAfterDays,
    ExitPartialAtR,
    ExitPctGainTarget,
    ExitRegime,
    ExitTimeStop,
    ExitTrailingSwingLow,
)
from app.hat.models import HatFill, HatOrder, HatPosition, HatSignal, HatTradeResult

D = Decimal
CENTS = st.integers(100, 100_000).map(lambda n: D(n) / 100)
FRACTIONS = st.sampled_from([0.1, 0.25, 0.33, 0.5, 0.75, 1.0])
ATR = st.sampled_from([0.5, 1.0, 1.5, 2.0, 3.0, 5.0])


@st.composite
def positions(draw: st.DrawFn) -> PositionState:
    entry = draw(st.integers(1000, 50_000).map(lambda n: D(n) / 100))
    initial = entry - draw(st.integers(1, 99)) * entry / 100
    stop = draw(st.sampled_from([initial, initial + (entry - initial) / 2, entry, entry + 1]))
    original = draw(st.integers(1, 1000))
    return PositionState(
        qty_original=original,
        qty_open=draw(st.integers(1, original)),
        avg_entry=entry,
        initial_stop=initial,
        current_stop=stop,
    )


@st.composite
def views(draw: st.DrawFn, pos: PositionState | None = None) -> ExitView:
    """A market view. Prices and levels are drawn from the thresholds the rules compare against
    (entry plus 0.5R to 3R, plus 5% to 50%, the stop, the entry itself) as well as at random, so
    equality at a threshold is common."""
    entry = pos.avg_entry if pos else D(100)
    risk = (entry - pos.initial_stop) if pos else D(5)
    edges = [entry, entry + 1, entry - 1]
    edges += [entry + r * risk for r in (D("0.5"), D(1), D(2), D(3))]
    edges += [entry * (1 + D(p) / 100) for p in (5, 10, 20, 50)]
    edges += [pos.current_stop, pos.initial_stop] if pos else []
    edges = [e for e in edges if e > 0]
    level = st.one_of(st.sampled_from(edges), st.integers(1, 40_000).map(lambda n: D(n) / 100))
    return ExitView(
        price=draw(st.one_of(st.none(), level, level)),
        days_held=draw(st.integers(0, 12)),
        sma=draw(st.fixed_dictionaries({10: level, 20: level, 50: level})),
        swing_low=draw(st.fixed_dictionaries({5: level, 10: level})),
        regime_off=draw(st.booleans()),
    )


RULE_KINDS = (
    st.just(ExitHardStop(rule="hard_stop")),
    st.builds(
        ExitBreakeven, rule=st.just("move_stop_to_breakeven"), at_r=st.sampled_from([0.5, 1.0, 2.0])
    ),
    st.just(ExitBreakeven(rule="move_stop_to_breakeven", after="partial")),
    st.builds(
        ExitPartialAtR,
        rule=st.just("partial_at_r"),
        at_r=st.sampled_from([0.5, 1.0, 2.0, 3.0]),
        sell_fraction=FRACTIONS,
    ),
    st.builds(
        ExitPartialAfterDays,
        rule=st.just("partial_after_days"),
        days=st.integers(1, 10),
        sell_fraction=FRACTIONS,
        only_if_profit=st.booleans(),
    ),
    st.builds(
        ExitCloseBelowSma,
        rule=st.just("close_below_sma"),
        sma=st.sampled_from([10, 20, 50]),
        sell_fraction=FRACTIONS,
    ),
    st.builds(
        ExitTrailingSwingLow,
        rule=st.just("trailing_swing_low"),
        lookback_days=st.sampled_from([5, 10]),
        sell_fraction=FRACTIONS,
    ),
    st.builds(
        ExitPctGainTarget,
        rule=st.just("pct_gain_target"),
        pct=st.sampled_from([5.0, 10.0, 20.0, 50.0]),
        sell_fraction=FRACTIONS,
    ),
    st.builds(
        ExitTimeStop,
        rule=st.just("time_stop"),
        days=st.integers(1, 10),
        min_r=st.sampled_from([0.0, 1.0, 2.0]),
    ),
    st.builds(
        ExitRegime, rule=st.just("regime_exit"), sell_fraction=FRACTIONS, enabled=st.booleans()
    ),
)
rules = st.one_of(*RULE_KINDS)
FULL_KINDS = st.one_of(
    st.builds(
        ExitCloseBelowSma,
        rule=st.just("close_below_sma"),
        sma=st.sampled_from([10, 20, 50]),
        sell_fraction=st.just(1.0),
    ),
    st.builds(
        ExitTimeStop,
        rule=st.just("time_stop"),
        days=st.integers(1, 3),
        min_r=st.sampled_from([5.0, 10.0]),
    ),
)
PARTIAL_KINDS = st.builds(
    ExitPartialAtR,
    rule=st.just("partial_at_r"),
    at_r=st.sampled_from([0.5, 1.0, 2.0]),
    sell_fraction=st.sampled_from([0.25, 0.5]),
)
stacks = st.lists(rules, min_size=1, max_size=8)
PHASES = st.sampled_from(["live", "close", "nightly", "close"])
FULL_RULES = (ExitHardStop, ExitTimeStop)


def is_full(rule: object) -> bool:
    return isinstance(rule, FULL_RULES) or getattr(rule, "sell_fraction", 0) >= 1.0


@given(stacks, positions(), st.data(), PHASES)
def test_full_beats_partial(
    stack: list, pos: PositionState, data: st.DataObject, phase: str
) -> None:
    """HAT-EXEC-070: when a full exit and partials fire on one evaluation, only the full exit is
    executed: the output is then exactly the one full exit the full rules alone give, for the
    whole open quantity. Whatever fires, the output never mixes a full exit with partials and
    never sells more than is open. Harm: a partial sale before a stop-out leaves the position
    half open after the trade should have ended."""
    # always put one full-capable and one partial rule in the stack, so both can fire together
    stack = data.draw(st.permutations([*stack, data.draw(FULL_KINDS), data.draw(PARTIAL_KINDS)]))
    view = data.draw(views(pos))
    if data.draw(st.booleans()):  # a view at which a full exit and a partial both fire
        gain = (pos.avg_entry - pos.initial_stop) * data.draw(st.sampled_from([D(1), D(2)]))
        price = pos.avg_entry + gain
        view = replace(view, price=price, days_held=5, sma=dict.fromkeys((10, 20, 50), price + 1))
        phase = "close"
    result = evaluate(stack, pos, view, phase)
    firing = [
        exits.rule_id(i, r)
        for i, r in enumerate(stack)
        if r.enabled and expected_phase(r, phase) and is_full(r) and expected_sells(r, pos, view)
    ]
    if firing:  # a full exit fires: it is the whole output, whatever partials also fire
        assert [(a.kind, a.qty) for a in result.actions] == [("full", pos.qty_open)]
        assert result.fired == (firing[0],)
        assert result.new_stop is None
    kinds = {a.kind for a in result.actions}
    assert kinds <= {"full"} or kinds == {"partial"}
    assert sum(a.qty for a in result.actions) <= pos.qty_open
    if "full" in kinds:
        assert len(result.actions) == 1
        assert result.actions[0].qty == pos.qty_open


@given(stacks, positions(), st.data(), st.lists(PHASES, min_size=1, max_size=8))
def test_fire_once(
    stack: list, pos: PositionState, data: st.DataObject, phase_list: list[str]
) -> None:
    """HAT-EXEC-071: over any sequence of evaluations a rule id is reported fired at most once,
    a fired rule never sells again, and the quantity sold never exceeds the original quantity.
    Harm: a partial rule that fires on every poll sells the whole position in slices."""
    seen: set[str] = set()
    sold = 0
    view = data.draw(views(pos))
    if data.draw(st.booleans()):  # a position a partial rule fires on, polled again and again
        stack = [*stack, data.draw(PARTIAL_KINDS)]
        pos = replace(pos, qty_original=1000, qty_open=1000, current_stop=pos.initial_stop)
        view = replace(view, price=pos.avg_entry + 2 * (pos.avg_entry - pos.initial_stop))
        phase_list = ["live"] * len(phase_list)
    start_open = pos.qty_open
    for phase in phase_list:
        view = data.draw(st.one_of(st.just(view), st.just(view), views(pos)))  # often the same
        before = pos
        result = evaluate(stack, pos, view, phase)
        assert not set(result.fired) & before.fired
        assert len(set(result.fired)) == len(result.fired)
        assert not set(result.fired) & seen
        seen |= set(result.fired)
        for action in result.actions:
            assert not set(action.rule_ids) & before.fired
        sold += sum(a.qty for a in result.actions)
        pos = exits.apply(pos, result)
        assert pos.qty_open == before.qty_open - sum(a.qty for a in result.actions) >= 0
    assert sold + pos.qty_open == start_open
    assert pos.qty_open >= 0


@given(
    st.integers(1, 10_000),
    st.integers(1, 10_000),
    st.sampled_from([0.01, 0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0]),
)
@example(3, 3, 0.33)
def test_fraction_arithmetic(original: int, open_: int, fraction: float) -> None:
    """HAT-EXEC-072: a fraction of the original quantity is rounded down, is at least one share
    and never more than the open quantity. Harm: a 1-share remainder that is never sold, or a
    sale larger than the position."""
    open_ = min(open_, original)
    got = exits.fraction_qty(fraction, original, open_)
    exact = Fraction(str(fraction)) * original
    assert got == min(max(1, exact.numerator // exact.denominator), open_)
    assert 1 <= got <= open_


@given(
    st.integers(1, 200),
    st.lists(st.sampled_from([0.1, 0.25, 0.33, 0.5, 0.75]), min_size=1, max_size=5),
)
def test_fraction_arithmetic_closes_with_last_exit(original: int, fractions: list[float]) -> None:
    """HAT-EXEC-072: partials that together reach the open quantity are one full exit, so the
    position closes with the last exit and no remainder below one share is left open."""
    stack = [
        ExitPartialAtR(rule="partial_at_r", at_r=0.5 * (i + 1), sell_fraction=f)
        for i, f in enumerate(fractions)
    ]
    pos = PositionState(original, original, D(100), D(90), D(90))
    view = ExitView(price=D(100) + 5 * len(fractions) + 5)
    result = evaluate(stack, pos, view, "live")
    qtys = [exits.fraction_qty(f, original, original) for f in fractions]
    if sum(qtys) >= original:
        assert [(a.kind, a.qty) for a in result.actions] == [("full", original)]
    else:
        assert [(a.kind, a.qty) for a in result.actions] == [("partial", q) for q in qtys]
        assert exits.apply(pos, result).qty_open == original - sum(qtys) >= 1


def expected_phase(rule: object, phase: str) -> bool:
    live = (ExitHardStop, ExitBreakeven, ExitPartialAtR, ExitPctGainTarget)
    close = (ExitPartialAfterDays, ExitCloseBelowSma, ExitTrailingSwingLow, ExitTimeStop)
    if phase == "live":
        return isinstance(rule, live)
    if phase == "close":
        return isinstance(rule, live + close)
    return isinstance(rule, ExitRegime)


def expected_sells(rule: object, pos: PositionState, view: ExitView) -> bool:
    """The firing condition of each selling rule, from the spec text."""
    price = view.price
    if isinstance(rule, ExitRegime):
        return view.regime_off  # decided on the index at night, no price of its own needed
    if price is None:
        return False
    entry, risk = Fraction(pos.avg_entry), Fraction(pos.avg_entry - pos.initial_stop)
    gain = Fraction(price) - entry
    if isinstance(rule, ExitHardStop):
        return price <= pos.current_stop
    if isinstance(rule, ExitPartialAtR):
        return gain >= Fraction(str(rule.at_r)) * risk
    if isinstance(rule, ExitPctGainTarget):
        return gain >= entry * Fraction(str(rule.pct)) / 100
    if isinstance(rule, ExitPartialAfterDays):
        return view.days_held >= rule.days and (not rule.only_if_profit or gain > 0)
    if isinstance(rule, ExitCloseBelowSma):
        return price < view.sma[rule.sma]
    if isinstance(rule, ExitTrailingSwingLow):
        return price < view.swing_low[rule.lookback_days]
    if isinstance(rule, ExitTimeStop):
        return view.days_held >= rule.days and gain < Fraction(str(rule.min_r)) * risk
    if isinstance(rule, ExitRegime):
        return view.regime_off
    return False


@pytest.mark.parametrize("kind", range(len(RULE_KINDS)))
@given(data=st.data())
def test_rule_semantics(kind: int, data: st.DataObject) -> None:
    rule = data.draw(RULE_KINDS[kind])
    pos = data.draw(positions())
    phase = data.draw(PHASES)
    check_rule_semantics(rule, pos, data, phase)


def check_rule_semantics(rule: object, pos: PositionState, data: st.DataObject, phase: str) -> None:
    """HAT-EXEC-073, HAT-EXEC-070: each rule alone sells (or raises the stop) exactly when the
    spec says, in the phase the spec gives it, and a disabled rule never acts; a rule never sees
    a missing price as a trigger. Harm: a rule firing early or late changes every result."""
    view = data.draw(views(pos))
    days = getattr(rule, "days", None)
    if days is not None:  # sit on the day threshold and either side of it
        view = replace(view, days_held=max(0, days + data.draw(st.integers(-1, 1))))
    result = evaluate([rule], pos, view, phase)
    live = getattr(rule, "enabled", True) and expected_phase(rule, phase)
    if isinstance(rule, ExitBreakeven):
        assert not result.actions
        due = (
            live
            and rule.at_r is not None
            and view.price is not None
            and (
                Fraction(view.price) - Fraction(pos.avg_entry)
                >= Fraction(str(rule.at_r)) * Fraction(pos.avg_entry - pos.initial_stop)
            )
        )
        if due and pos.avg_entry > pos.current_stop:
            assert result.new_stop == pos.avg_entry
        else:
            assert result.new_stop is None
        return
    sells = bool(live and expected_sells(rule, pos, view))
    assert bool(result.actions) == sells
    if sells:
        fraction = getattr(rule, "sell_fraction", 1.0) if not is_full(rule) else 1.0
        want = (
            pos.qty_open
            if is_full(rule)
            else exits.fraction_qty(fraction, pos.qty_original, pos.qty_open)
        )
        assert result.actions[0].qty == want
        assert result.fired == (exits.rule_id(0, rule),)
    else:
        assert not result.fired
        assert result.new_stop is None


@given(positions(), st.data(), st.booleans(), st.sampled_from([0.25, 0.5]))
def test_rule_semantics_breakeven_after_partial(
    pos: PositionState, data: st.DataObject, earlier: bool, fraction: float
) -> None:
    """HAT-EXEC-073: `after: partial` raises the stop to the entry price once a partial rule has
    fired, in this evaluation or an earlier one, and not before."""
    stack = [
        ExitPartialAtR(rule="partial_at_r", at_r=1.0, sell_fraction=fraction),
        ExitBreakeven(rule="move_stop_to_breakeven", after="partial"),
    ]
    fired = frozenset({"0:partial_at_r"}) if earlier else frozenset()
    pos = PositionState(
        pos.qty_original, pos.qty_open, pos.avg_entry, pos.initial_stop, pos.current_stop, fired
    )
    view = data.draw(views(pos))
    result = evaluate(stack, pos, view, "live")
    partial_now = any(a.rule_ids == ("0:partial_at_r",) for a in result.actions) or (
        len(result.actions) == 1 and "0:partial_at_r" in result.actions[0].rule_ids
    )
    closing = any(a.kind == "full" for a in result.actions)
    due = (earlier or partial_now) and not closing
    if due and pos.avg_entry > pos.current_stop:
        assert result.new_stop == pos.avg_entry
    else:
        assert result.new_stop is None


@given(stacks, positions(), st.data(), st.lists(PHASES, min_size=1, max_size=10))
def test_never_lowers_stop(
    stack: list, pos: PositionState, data: st.DataObject, phase_list: list[str]
) -> None:
    """HAT-RISK-070: over any run of evaluations the stop never goes down; a new stop is always
    above the current one; and a result that carries a lower stop changes nothing. Harm: a
    lower stop widens the risk the position was sized for."""
    for phase in phase_list:
        result = evaluate(stack, pos, data.draw(views(pos)), phase)
        if result.new_stop is not None:
            assert result.new_stop > pos.current_stop
        after = exits.apply(pos, result)
        assert after.current_stop >= pos.current_stop
        pos = after
    lowered = exits.Evaluation(new_stop=pos.current_stop - 1)
    assert exits.apply(pos, lowered).current_stop == pos.current_stop


# --- the shadow engine, end to end -----------------------------------------------------------

paths = st.lists(
    st.tuples(st.integers(60, 160), st.integers(0, 3)),
    min_size=1,
    max_size=12,
)
SHADOW_STACKS = st.sampled_from(["trend", "partials", "stop_only"])


def run_path(session: Session, kind: str, path: list[tuple[int, int]]) -> Trade:
    world = seed_engine_world(session, exits=exit_stack(kind))
    trade = open_trade(session, world)
    assert trade.outcome.position_id is not None
    for step, (price, days) in enumerate(path):
        step_trade(session, trade, D(price), days=days, minutes=step)
        if session.get(HatPosition, trade.outcome.position_id).closed_at is not None:
            break
    return trade


@settings(max_examples=40)
@given(SHADOW_STACKS, paths)
def test_shadow_rows_virtual(kind: str, path: list[tuple[int, int]]) -> None:
    """HAT-EXEC-010, HAT-EXEC-044: a shadow trade driven from signal to close writes only virtual
    orders, fills and positions, with no broker order id, no broker trade id and no fees (the
    engine imports no broker at all: CONF import_graph_boundaries); the sold quantity plus what
    stays open is the entry quantity; and
    a closed position has exactly one trade result whose R is its P&L over the initial risk.
    Harm: a shadow row that looks real can be reconciled against, and sold at, the broker."""
    engine = memory_engine()
    with Session(engine) as session:
        trade = run_path(session, kind, path)
        session.flush()
        orders = session.scalars(select(HatOrder)).all()
        fills = session.scalars(select(HatFill)).all()
        position = session.get(HatPosition, trade.outcome.position_id)
        assert orders
        assert all(o.is_virtual for o in orders)
        assert all(o.broker_order_id is None for o in orders)
        assert fills
        assert all(f.is_virtual for f in fills)
        assert all(f.broker_trade_id is None for f in fills)
        assert all(f.fees == 0 for f in fills)
        assert position.is_virtual
        bought = sum(o.qty for o in orders if o.side == "buy")
        sold = sum(o.qty for o in orders if o.side == "sell")
        assert sold + position.qty_open == bought
        results = session.scalars(select(HatTradeResult)).all()
        event(f"closed={position.closed_at is not None} partial={sold > 0}")
        if position.closed_at is None:
            assert not results
            assert position.qty_open > 0
            return
        assert position.qty_open == 0
        assert len(results) == 1
        pnl = sum(
            (f.qty * Decimal(f.price) * (1 if o.side == "sell" else -1))
            for o, f in session.execute(
                select(HatOrder, HatFill).join(HatFill, HatFill.order_id == HatOrder.id)
            ).all()
        )
        risk = (Decimal(position.avg_entry) - Decimal(position.initial_stop)) * bought
        assert results[0].realized_pnl == pnl
        assert results[0].r_multiple == (pnl / risk).quantize(Decimal("0.000001"))


@settings(max_examples=40)
@given(SHADOW_STACKS, paths)
def test_replay_reproduces_state(kind: str, path: list[tuple[int, int]]) -> None:
    """HAT-EXEC-042: after every step of a shadow trade the state stored on the signal (and its
    position) equals the fold of the trade's events over the transition table, and an event the
    table does not allow changes nothing. Harm: a state that cannot be rebuilt from the log hides
    a missed event."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, exits=exit_stack(kind))
        trade = open_trade(session, world)
        signal = session.get(HatSignal, trade.outcome.signal_id)
        position = session.get(HatPosition, trade.outcome.position_id)
        assert (
            trades.replay_state(session, signal.id)
            == signal.state
            == position.state
            == lifecycle.OPEN
        )
        for step, (price, days) in enumerate(path):
            step_trade(session, trade, D(price), days=days, minutes=step)
            session.refresh(signal)
            session.refresh(position)
            assert trades.replay_state(session, signal.id) == signal.state
            if position.state in lifecycle.HOLDING_STATES or position.state == lifecycle.CLOSED:
                assert position.state == signal.state
            if position.closed_at is not None:
                assert signal.state == lifecycle.CLOSED
                break
        # A step the table does not allow from the reached state is refused, and the stored
        # state still equals the fold of the log.
        with pytest.raises(lifecycle.LifecycleStateError):
            lifecycle.next_state(signal.state, "signal.triggered")
        assert trades.replay_state(session, signal.id) == signal.state
