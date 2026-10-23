"""PBT exits.*: the exit stack of
`app.hat.engine.exits` against rules written out independently, and the shadow engine of
`app.hat.engine.trades` driven end to end on SQLite with a broker that records every call."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.engine import exits
from app.hat.engine.approvals import veto_score, veto_verdict
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.positions import TradeError, raise_stop
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
from app.hat.models import HatPosition, HatSignal

D = Decimal
FRACTIONS = st.sampled_from([0.1, 0.25, 0.33, 0.5, 0.75, 1.0])


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
    """When a full exit and partials fire on one evaluation, only the full exit is
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
    """Over any sequence of evaluations a rule id is reported fired at most once,
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
    """A fraction of the original quantity is rounded down, is at least one share
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
    """Partials that together reach the open quantity are one full exit, so the
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
    """Each rule alone sells (or raises the stop) exactly when the
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
    """`after: partial` raises the stop to the entry price once a partial rule has
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
    """Over any run of evaluations the stop never goes down; a new stop is always
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
R_MULTIPLES = st.decimals(
    min_value=-50, max_value=50, places=4, allow_nan=False, allow_infinity=False
)


@settings(max_examples=100)
@given(st.lists(R_MULTIPLES, max_size=30))
@example([])
@example([Decimal(0)])
def test_veto_score(results: list[Decimal]) -> None:
    """The veto score over the R multiples of the closed veto trades is
    avoided = -sum(R < 0) and missed = sum(R > 0), exactly, with a zero R counted as a trade in
    neither sum; both are non-negative and missed - avoided is the plain sum. Harm: the weekly
    digest tells the user their skips saved money they in fact cost."""
    score = veto_score(results)
    losses = [Fraction(r) for r in results if r < 0]
    gains = [Fraction(r) for r in results if r > 0]
    assert Fraction(score.avoided_r) == -sum(losses, Fraction(0))
    assert Fraction(score.missed_r) == sum(gains, Fraction(0))
    assert score.trades == len(results)
    assert score.avoided_r >= 0
    assert score.missed_r >= 0
    assert Fraction(score.missed_r - score.avoided_r) == sum(map(Fraction, results), Fraction(0))


@settings(max_examples=200)
@given(
    st.integers(min_value=0, max_value=120),
    st.lists(R_MULTIPLES, max_size=20),
    st.lists(R_MULTIPLES, max_size=20),
)
@example(49, [Decimal(1)], [Decimal(2)])
@example(50, [], [Decimal(1)])
@example(50, [Decimal("0.1"), Decimal("0.2")], [Decimal("0.15")])
def test_veto_verdict_threshold(
    decisions: int, skipped: list[Decimal], taken: list[Decimal]
) -> None:
    """The verdict exists iff there are at least 50 decisions; it compares the
    mean R of the skipped trades with the mean R of the taken ones, exactly, and says the skips
    beat the takes iff the skipped trades did worse; a group with no closed trade has no mean
    and the verdict is insufficient data. Harm: a verdict drawn from too few decisions, a
    division by zero, or a rounding that turns the verdict around."""
    verdict = veto_verdict(decisions, skipped, taken)
    if decisions < 50:
        assert verdict is None
        return
    assert verdict is not None
    assert verdict.decisions == decisions
    if not skipped or not taken:
        assert verdict.verdict == "insufficient data"
        assert (verdict.skipped_mean is None) == (not skipped)
        assert (verdict.taken_mean is None) == (not taken)
        return
    skipped_mean = sum(map(Fraction, skipped), Fraction(0)) / len(skipped)
    taken_mean = sum(map(Fraction, taken), Fraction(0)) / len(taken)
    assert abs(Fraction(verdict.skipped_mean) - skipped_mean) < Fraction(1, 10**20)
    assert abs(Fraction(verdict.taken_mean) - taken_mean) < Fraction(1, 10**20)
    expected = "skips beat takes" if skipped_mean < taken_mean else "skips do not beat takes"
    assert verdict.verdict == expected


@pytest.mark.parametrize("new", [D("99.95"), D("100.00")])
def test_raise_stop_refuses_a_stop_that_does_not_rise(new: Decimal) -> None:
    """`positions.raise_stop` is invariant 1's Python check: a stop at or below the current one
    is refused before anything is written (the session is never touched)."""
    position = HatPosition(current_stop=D("100.00"), is_virtual=True)
    with pytest.raises(TradeError) as refused:
        raise_stop(None, HatSignal(mode="shadow"), position, new, None)  # type: ignore[arg-type]
    assert refused.value.code == "stop_would_lower"
    assert position.current_stop == D("100.00")
