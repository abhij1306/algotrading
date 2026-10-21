"""The exit stack (07 §7, HAT-EXEC-070..073, HAT-RISK-070, HAT-DEC-B19): pure and exact.

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from .strategy.config import (
    ExitBreakeven,
    ExitCloseBelowSma,
    ExitHardStop,
    ExitPartialAfterDays,
    ExitPartialAtR,
    ExitPctGainTarget,
    ExitRegime,
    ExitRule,
    ExitTimeStop,
    ExitTrailingSwingLow,
)

__all__ = [
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
]

Phase = Literal["live", "close", "nightly"]
IGNORED_NOT_RAISING = "would_not_raise_stop"
# The phases whose rules an evaluation in a given phase reads: the 15:25 check also sees the
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
    "trailing_swing_low": "close",
    "time_stop": "close",
    "regime_exit": "nightly",
}


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


def rule_id(index: int, rule: ExitRule) -> str:
    """The stable id of the rule at `index` of the stack (the stack is pinned per trade)."""
    return f"{index}:{rule.rule}"


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def fraction_qty(fraction: float, qty_original: int, qty_open: int) -> int:
    """max(1, floor(fraction x original quantity)), never more than the open quantity."""
    sold = int((_dec(fraction) * qty_original).to_integral_value(rounding=ROUND_FLOOR))
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
    return view.price is not None and risk > 0 and view.price - pos.avg_entry >= _dec(at_r) * risk


def _sells(rule: ExitRule, pos: PositionState, view: ExitView) -> bool:
    """Whether a rule that sells fires on this view. Missing data never fires a rule."""
    price = view.price
    if isinstance(rule, ExitHardStop):
        return price is not None and price <= pos.current_stop
    if isinstance(rule, ExitPartialAtR):
        return _reached_r(view, pos, rule.at_r)
    if isinstance(rule, ExitPctGainTarget):
        return price is not None and (price - pos.avg_entry) * 100 >= _dec(rule.pct) * pos.avg_entry
    if isinstance(rule, ExitPartialAfterDays):
        if price is None or view.days_held < rule.days:
            return False
        return not rule.only_if_profit or price > pos.avg_entry
    if isinstance(rule, ExitCloseBelowSma):
        level = view.sma.get(rule.sma)
        return price is not None and level is not None and price < level
    if isinstance(rule, ExitTrailingSwingLow):
        level = view.swing_low.get(rule.lookback_days)
        return price is not None and level is not None and price < level
    if isinstance(rule, ExitTimeStop):
        if price is None or view.days_held < rule.days:
            return False
        risk = pos.avg_entry - pos.initial_stop
        return risk > 0 and (price - pos.avg_entry) < _dec(rule.min_r) * risk
    if isinstance(rule, ExitRegime):
        return view.regime_off
    return False


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


def evaluate(
    rules: Sequence[ExitRule], pos: PositionState, view: ExitView, phase: Phase
) -> Evaluation:
    """What the stack does for `pos` at `view` in `phase` (HAT-EXEC-070..073)."""
    if pos.qty_open <= 0:
        return Evaluation()
    allowed = _PHASES[phase]
    live = [
        (index, rule_id(index, rule), rule)
        for index, rule in enumerate(rules)
        if rule.enabled and _RULE_PHASE[rule.rule] in allowed
    ]
    live = [(i, rid, rule) for i, rid, rule in live if rid not in pos.fired]
    full_hits = [(rid, rule) for _, rid, rule in live if _is_full(rule) and _sells(rule, pos, view)]
    if full_hits:
        # Full beats partial: one exit of everything left; only the rules behind it are fired.
        first = full_hits[0][0]
        return Evaluation(actions=(ExitAction("full", pos.qty_open, (first,)),), fired=(first,))
    partial_hits = [
        (rid, rule)
        for _, rid, rule in live
        if not isinstance(rule, ExitBreakeven) and is_partial_rule(rule) and _sells(rule, pos, view)
    ]
    qtys = [
        fraction_qty(_fraction_of(rule) or 1.0, pos.qty_original, pos.qty_open)
        for _, rule in partial_hits
    ]
    ids = tuple(rid for rid, _ in partial_hits)
    actions: tuple[ExitAction, ...] = ()
    if partial_hits and sum(qtys) >= pos.qty_open:
        actions = (ExitAction("full", pos.qty_open, ids),)
    elif partial_hits:
        actions = tuple(
            ExitAction("partial", qty, (rid,))
            for (rid, _), qty in zip(partial_hits, qtys, strict=True)
        )
    fired = list(ids)
    ignored: list[tuple[str, str]] = []
    new_stop: Decimal | None = None
    if not actions or actions[0].kind != "full":
        done = any(is_partial_rule(rules[int(rid.split(":", 1)[0])]) for rid in (*pos.fired, *ids))
        for _, rid, rule in live:
            if isinstance(rule, ExitBreakeven) and _breakeven_due(rule, pos, view, done):
                fired.append(rid)
                if pos.avg_entry > pos.current_stop and (
                    new_stop is None or pos.avg_entry > new_stop
                ):
                    new_stop = pos.avg_entry
                else:
                    ignored.append((rid, IGNORED_NOT_RAISING))
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
