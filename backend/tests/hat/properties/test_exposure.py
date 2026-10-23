"""PBT exposure.*: the L1 to L3 ladder on the real code."""

from hat_risk_support import D
from hypothesis import given
from hypothesis import strategies as st

from app.hat.risk.exposure import (
    ClosedTrade,
    LadderState,
    ladder_from_trades,
)
from app.hat.risk.policy import LEVELS, seed_policy

SEED = seed_policy()
R = st.decimals(min_value=D("-3"), max_value=D("6"), places=2)
trade_lists = st.lists(st.builds(ClosedTrade, R, st.booleans()), max_size=60)


def oracle(trades: list[ClosedTrade], up: int, down: int) -> tuple[int, int, int]:
    """Written independently of the module: (level index, wins run, losses run)."""
    level, wins, losses = 0, 0, 0
    for t in trades:
        if t.is_virtual:
            continue
        if t.r_multiple > 0:
            wins, losses = wins + 1, 0
            if wins >= up and level < 2:
                level, wins = level + 1, 0
        else:
            losses, wins = losses + 1, 0
            if losses >= down and level > 0:
                level, losses = level - 1, 0
    return level, wins, losses


@given(
    trade_lists, st.lists(st.decimals(min_value=D("0.1"), max_value=D(4), places=2), max_size=30)
)
def test_virtual_ignored(trades: list, shadow_wins: list) -> None:
    """Shadow trades, wins or losses, anywhere in the sequence leave the ladder where the real
    trades alone put it."""
    real = [t for t in trades if not t.is_virtual]
    mixed = []
    shadows = [ClosedTrade(r, True) for r in shadow_wins]
    for index, t in enumerate(real):
        mixed.append(t)
        if index < len(shadows):
            mixed.append(shadows[index])
    mixed.extend(shadows[len(real) :])
    assert ladder_from_trades(mixed, SEED) == ladder_from_trades(real, SEED)
    assert ladder_from_trades(shadows, SEED) == LadderState()


@given(st.lists(st.just(ClosedTrade(D("1"), False)), max_size=20))
def test_level_never_exceeds_l3(wins: list) -> None:
    assert ladder_from_trades(wins, SEED).level in LEVELS
    assert LEVELS.index(ladder_from_trades(wins, SEED).level) <= 2
