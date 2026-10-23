"""Progressive exposure: the ladder L1 to L3 (HAT-RISK-040/041/042).

A bot starts at L1 and moves up one level after `up_after_wins` consecutive winners and down one
after `down_after_losses` consecutive losers. A level move resets both counters; an outcome of
the opposite kind resets the other counter. A trade that closes at 0 R or worse counts as a
loss (the conservative reading). Only real closed trades move the ladder: a shadow (virtual)
trade is ignored, so paper wins never unlock real exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .policy import LEVELS, RiskPolicy

START_LEVEL = LEVELS[0]


@dataclass(frozen=True)
class LadderState:
    level: str = START_LEVEL
    consecutive_wins: int = 0
    consecutive_losses: int = 0


@dataclass(frozen=True)
class ClosedTrade:
    r_multiple: Decimal
    is_virtual: bool


def is_win(r_multiple: Decimal) -> bool:
    return r_multiple > 0


def apply_outcome(state: LadderState, win: bool, policy: RiskPolicy) -> LadderState:
    """The ladder state after one real closed trade."""
    index = LEVELS.index(state.level)
    if win:
        wins, losses = state.consecutive_wins + 1, 0
        if wins >= policy.up_after_wins and index < len(LEVELS) - 1:
            return LadderState(LEVELS[index + 1], 0, 0)
        return LadderState(state.level, wins, losses)
    losses, wins = state.consecutive_losses + 1, 0
    if losses >= policy.down_after_losses and index > 0:
        return LadderState(LEVELS[index - 1], 0, 0)
    return LadderState(state.level, wins, losses)


def apply_trade(state: LadderState, trade: ClosedTrade, policy: RiskPolicy) -> LadderState:
    """`apply_outcome` for a closed trade; a virtual trade leaves the state unchanged."""
    if trade.is_virtual:
        return state
    return apply_outcome(state, is_win(trade.r_multiple), policy)


def max_positions(level: str, policy: RiskPolicy) -> int:
    """The open-position limit at `level`: the lesser of the policy maximum and the level's."""
    return min(policy.max_open_positions, policy.ladder[level].max_positions)


def risk_multiplier(level: str, policy: RiskPolicy) -> Decimal:
    return policy.ladder[level].risk_mult
