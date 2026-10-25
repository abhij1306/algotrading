"""The exit stack (HAT-EXEC-070..073, HAT-RISK-070, HAT-DEC-B19): pure and exact.

`evaluate` reads one position, one price view and the bot's ordered rules, and returns what to
do: at most one full exit or an ordered list of partials, and a stop that can only rise. It
changes nothing; `apply` returns the position after those actions. Live trading, the shadow
engine and the backtest all call these two functions (T12).

Evaluation rules, as written in the spec:
- rules are read top to bottom; a rule already in `fired` never fires again;
- a full exit beats every partial that fires on the same evaluation (the output is then exactly
  one exit of the whole open quantity, and the partials stay unfired);
- fractions apply to the original quantity, round down to at least one share and are capped at
  the open quantity; partials that together reach the open quantity become one full exit;
- a rule that would not raise the stop is ignored and logged, never applied;
- an exit never waits for the user.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from ..money import require_exact
from .criteria.indicators import sma
from .stops import lowest
from .strategy.config import (
    ExitBreakeven,
    ExitCloseAboveSma,
    ExitCloseBelowSma,
    ExitHardStop,
    ExitPartialAfterDays,
    ExitPartialAtR,
    ExitPctGainTarget,
    ExitRegime,
    ExitRule,
    ExitTimeStop,
    ExitTrailingSwingLow,
    ExitVsSma,
)

__all__ = [
    "CLOSE_PHASE_RULES",
    "IGNORED_NOT_RAISING",
    "Evaluation",
    "ExitAction",
    "ExitView",
    "Phase",
    "PositionState",
    "apply",
    "evaluate",
    "fraction_qty",
    "is_partial_rule",
    "r_multiple",
    "rule_id",
    "history_sessions",
    "view_from_bars",
]

Phase = Literal["live", "close", "nightly"]
IGNORED_NOT_RAISING = "would_not_raise_stop"
# The phases whose rules an evaluation in a given phase reads: the 15:05-15:10 check also sees the
# live price, so it reads live rules too.
_PHASES: dict[str, frozenset[str]] = {
    "live": frozenset({"live"}),
    "close": frozenset({"live", "close"}),
    "nightly": frozenset({"nightly"}),
}
_RULE_PHASE = {
    "hard_stop": "live",
    "move_stop_to_breakeven": "live",
    "partial_at_r": "live",
    "pct_gain_target": "live",
    "partial_after_days": "close",
    "close_below_sma": "close",
    "close_above_sma": "close",
    "trailing_swing_low": "close",
    "time_stop": "close",
    "regime_exit": "nightly",
}
# The rules the close phase runs: an exit by one of them is priced at the close-phase price.
CLOSE_PHASE_RULES = frozenset(rule for rule, phase in _RULE_PHASE.items() if phase == "close")


@dataclass(frozen=True)
class PositionState:
    """A long position as the exit rules read it. `fired` holds rule ids (`rule_id`)."""

    qty_original: int
    qty_open: int
    avg_entry: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    fired: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExitView:
    """Market facts at one evaluation. A fact left out makes the rules that need it not fire."""

    price: Decimal | None
    days_held: int = 0
    sma: Mapping[int, Decimal] = field(default_factory=dict)
    swing_low: Mapping[int, Decimal] = field(default_factory=dict)
    regime_off: bool = False


@dataclass(frozen=True)
class ExitAction:
    """One sell: `kind` full (the whole open quantity) or partial, for `qty` shares."""

    kind: Literal["full", "partial"]
    qty: int
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    actions: tuple[ExitAction, ...] = ()
    new_stop: Decimal | None = None  # a higher stop, or None for unchanged
    fired: tuple[str, ...] = ()  # ids newly marked fired, in stack order
    ignored: tuple[tuple[str, str], ...] = ()  # (rule id, reason), logged by the caller


def history_sessions(rules: Sequence[ExitRule]) -> int:
    """How many sessions before the decision session `view_from_bars` reads for `rules`."""
    needs = [rule.sma - 1 for rule in rules if isinstance(rule, ExitVsSma)]
    needs += [rule.lookback_days for rule in rules if isinstance(rule, ExitTrailingSwingLow)]
    return max(needs, default=0)


def view_from_bars(
    price: Decimal | None,
    rules: Sequence[ExitRule],
    days_held: int,
    closes: Sequence[Decimal],
    lows: Sequence[Decimal],
) -> ExitView:
    """The view of a position at `price` with the SMA and swing-low levels its `rules` read, from
    the `closes` and `lows` of the sessions the instrument traded before the decision session
    (oldest first, at least `history_sessions(rules)` of them for every level to exist). `price`
    stands in for the decision session's close in each SMA (the live price, or the backtest's
    daily close, HAT-EXEC-074); the swing low is the lowest of the last `lookback_days` lows
    before it. A level without enough sessions is left out, and its rule does not fire. The
    live close phase and the backtest both build their views here, so the rules read the same
    inputs in both."""
    sma_levels: dict[int, Decimal] = {}
    swing: dict[int, Decimal] = {}
    for rule in rules:
        if isinstance(rule, ExitVsSma) and price is not None:
            prior = list(closes[-(rule.sma - 1) :]) if rule.sma > 1 else []
            if (level := sma([*prior, price], rule.sma)) is not None:
                sma_levels[rule.sma] = level
        elif isinstance(rule, ExitTrailingSwingLow):
            n = rule.lookback_days
            if (low := lowest(lows[-n:], n)) is not None:
                swing[n] = low
    return ExitView(price, days_held=days_held, sma=sma_levels, swing_low=swing)


def rule_id(index: int, rule: ExitRule) -> str:
    """The stable id of the rule at `index` of the stack (the stack is pinned per trade)."""
    return f"{index}:{rule.rule}"


def fraction_qty(fraction: float, qty_original: int, qty_open: int) -> int:
    """max(1, floor(fraction x original quantity)), never more than the open quantity."""
    sold = int(
        (require_exact(fraction, "fraction", floats=True) * qty_original).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return min(max(1, sold), qty_open)


def r_multiple(price: Decimal, avg_entry: Decimal, initial_stop: Decimal) -> Decimal | None:
    """(price - avg_entry) / (avg_entry - initial_stop); None without positive initial risk."""
    risk = avg_entry - initial_stop
    if risk <= 0:
        return None
    return (price - avg_entry) / risk


def _fraction_of(rule: ExitRule) -> float | None:
    """The sell fraction of a rule that sells one; None for rules that do not."""
    return getattr(rule, "sell_fraction", None)


def is_partial_rule(rule: ExitRule) -> bool:
    """A rule that sells less than the whole position when it fires."""
    fraction = _fraction_of(rule)
    return fraction is not None and fraction < 1.0


def _reached_r(view: ExitView, pos: PositionState, at_r: float) -> bool:
    """R >= at_r, compared without dividing: price - entry >= at_r x (entry - initial stop)."""
    risk = pos.avg_entry - pos.initial_stop
    return (
        view.price is not None
        and risk > 0
        and view.price - pos.avg_entry >= require_exact(at_r, "at_r", floats=True) * risk
    )


def _price_at_or_below_stop(rule: ExitHardStop, pos: PositionState, view: ExitView) -> bool:
    return view.price is not None and view.price <= pos.current_stop


def _partial_r(rule: ExitPartialAtR, pos: PositionState, view: ExitView) -> bool:
    return _reached_r(view, pos, rule.at_r)


def _gain_target(rule: ExitPctGainTarget, pos: PositionState, view: ExitView) -> bool:
    price = view.price
    return (
        price is not None
        and (price - pos.avg_entry) * 100
        >= require_exact(rule.pct, "pct", floats=True) * pos.avg_entry
    )


def _after_days(rule: ExitPartialAfterDays, pos: PositionState, view: ExitView) -> bool:
    price = view.price
    if price is None or view.days_held < rule.days:
        return False
    return not rule.only_if_profit or price > pos.avg_entry


def _below_sma(rule: ExitCloseBelowSma, pos: PositionState, view: ExitView) -> bool:
    level = view.sma.get(rule.sma)
    return view.price is not None and level is not None and view.price < level


def _above_sma(rule: ExitCloseAboveSma, pos: PositionState, view: ExitView) -> bool:
    level = view.sma.get(rule.sma)
    return view.price is not None and level is not None and view.price > level


def _below_swing_low(rule: ExitTrailingSwingLow, pos: PositionState, view: ExitView) -> bool:
    level = view.swing_low.get(rule.lookback_days)
    return view.price is not None and level is not None and view.price < level


def _time_stop(rule: ExitTimeStop, pos: PositionState, view: ExitView) -> bool:
    price = view.price
    if price is None or view.days_held < rule.days:
        return False
    risk = pos.avg_entry - pos.initial_stop
    return (
        risk > 0
        and (price - pos.avg_entry) < require_exact(rule.min_r, "min_r", floats=True) * risk
    )


def _regime_off(rule: ExitRegime, pos: PositionState, view: ExitView) -> bool:
    return view.regime_off


# The firing test of each rule that sells. Missing data never fires a rule.
_SELL_TESTS: dict[type, Callable[[Any, PositionState, ExitView], bool]] = {
    ExitHardStop: _price_at_or_below_stop,
    ExitPartialAtR: _partial_r,
    ExitPctGainTarget: _gain_target,
    ExitPartialAfterDays: _after_days,
    ExitCloseBelowSma: _below_sma,
    ExitCloseAboveSma: _above_sma,
    ExitTrailingSwingLow: _below_swing_low,
    ExitTimeStop: _time_stop,
    ExitRegime: _regime_off,
}


def _sells(rule: ExitRule, pos: PositionState, view: ExitView) -> bool:
    """Whether a rule that sells fires on this view. Missing data never fires a rule."""
    test = _SELL_TESTS.get(type(rule))
    return test is not None and test(rule, pos, view)


def _is_full(rule: ExitRule) -> bool:
    """hard_stop and time_stop always sell everything; the others when their fraction is 1."""
    if isinstance(rule, ExitHardStop | ExitTimeStop):
        return True
    fraction = _fraction_of(rule)
    return fraction is not None and fraction >= 1.0


def _breakeven_due(
    rule: ExitBreakeven, pos: PositionState, view: ExitView, partial_done: bool
) -> bool:
    if rule.after == "partial":
        return partial_done
    return rule.at_r is not None and _reached_r(view, pos, rule.at_r)


_Live = list[tuple[int, str, ExitRule]]


def _live_rules(rules: Sequence[ExitRule], pos: PositionState, phase: Phase) -> _Live:
    """The enabled, not yet fired rules that run in `phase`, in stack order."""
    allowed = _PHASES[phase]
    return [
        (index, rid, rule)
        for index, rule in enumerate(rules)
        if rule.enabled
        and _RULE_PHASE[rule.rule] in allowed
        and (rid := rule_id(index, rule)) not in pos.fired
    ]


def _partial_actions(
    live: _Live, pos: PositionState, view: ExitView
) -> tuple[tuple[ExitAction, ...], tuple[str, ...]]:
    """The partial sells that fire, merged into one full exit when they cover what is left."""
    hits = [
        (rid, rule)
        for _, rid, rule in live
        if not isinstance(rule, ExitBreakeven) and is_partial_rule(rule) and _sells(rule, pos, view)
    ]
    qtys = [
        fraction_qty(_fraction_of(rule) or 1.0, pos.qty_original, pos.qty_open) for _, rule in hits
    ]
    ids = tuple(rid for rid, _ in hits)
    if not hits:
        return (), ids
    if sum(qtys) >= pos.qty_open:
        return (ExitAction("full", pos.qty_open, ids),), ids
    actions = tuple(
        ExitAction("partial", qty, (rid,)) for (rid, _), qty in zip(hits, qtys, strict=True)
    )
    return actions, ids


def _breakevens(
    rules: Sequence[ExitRule],
    live: _Live,
    pos: PositionState,
    view: ExitView,
    ids: tuple[str, ...],
) -> tuple[list[str], Decimal | None, list[tuple[str, str]]]:
    """The breakeven moves that are due: their ids, the stop they raise to (only ever up) and
    the ones ignored because they would not raise it (HAT-RISK-070)."""
    done = any(is_partial_rule(rules[int(rid.split(":", 1)[0])]) for rid in (*pos.fired, *ids))
    fired: list[str] = []
    ignored: list[tuple[str, str]] = []
    new_stop: Decimal | None = None
    for _, rid, rule in live:
        if not (isinstance(rule, ExitBreakeven) and _breakeven_due(rule, pos, view, done)):
            continue
        fired.append(rid)
        if pos.avg_entry > pos.current_stop and (new_stop is None or pos.avg_entry > new_stop):
            new_stop = pos.avg_entry
        else:
            ignored.append((rid, IGNORED_NOT_RAISING))
    return fired, new_stop, ignored


def evaluate(
    rules: Sequence[ExitRule], pos: PositionState, view: ExitView, phase: Phase
) -> Evaluation:
    """What the stack does for `pos` at `view` in `phase` (HAT-EXEC-070..073)."""
    if pos.qty_open <= 0:
        return Evaluation()
    live = _live_rules(rules, pos, phase)
    full_hits = [rid for _, rid, rule in live if _is_full(rule) and _sells(rule, pos, view)]
    if full_hits:
        # Full beats partial: one exit of everything left; only the rules behind it are fired.
        first = full_hits[0]
        return Evaluation(actions=(ExitAction("full", pos.qty_open, (first,)),), fired=(first,))
    actions, ids = _partial_actions(live, pos, view)
    fired = list(ids)
    ignored: list[tuple[str, str]] = []
    new_stop: Decimal | None = None
    if not actions or actions[0].kind != "full":
        moved, new_stop, ignored = _breakevens(rules, live, pos, view, ids)
        fired.extend(moved)
    order = {rid: n for n, (_, rid, _) in enumerate(live)}
    return Evaluation(
        actions=actions,
        new_stop=new_stop,
        fired=tuple(sorted(set(fired), key=lambda r: order[r])),
        ignored=tuple(ignored),
    )


def apply(pos: PositionState, result: Evaluation) -> PositionState:
    """`pos` after `result`: the sold quantity gone, the new stop (never lower) and fired ids."""
    sold = sum(action.qty for action in result.actions)
    stop = pos.current_stop
    if result.new_stop is not None and result.new_stop > stop:
        stop = result.new_stop
    return PositionState(
        qty_original=pos.qty_original,
        qty_open=max(0, pos.qty_open - sold),
        avg_entry=pos.avg_entry,
        initial_stop=pos.initial_stop,
        current_stop=stop,
        fired=pos.fired | frozenset(result.fired),
    )
