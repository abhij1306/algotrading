"""PBT exposure.* (HAT-DEC-B16, HAT-RISK-040/041/042): the L1 to L3 ladder on the real code."""

from decimal import Decimal

from hat_risk_support import BOT, D, decide, policy_with, position, valid_ctx
from hypothesis import given
from hypothesis import strategies as st

from app.hat.risk.decision import Reason
from app.hat.risk.exposure import (
    ClosedTrade,
    LadderState,
    apply_trade,
    ladder_from_trades,
    max_positions,
    risk_multiplier,
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


@given(trade_lists, st.integers(1, 5), st.integers(1, 5))
def test_ladder_transitions(trades: list, up: int, down: int) -> None:
    """From L1, `up_after_wins` consecutive winners move up one level and `down_after_losses`
    consecutive losers move down one, never above L3 or below L1; a level move resets both
    counters, and an outcome of the other kind resets the opposite counter."""
    policy = policy_with(
        exposure_ladder={
            **SEED.to_json()["exposure_ladder"],
            "up_after_wins": up,
            "down_after_losses": down,
        }
    )
    state = ladder_from_trades(trades, policy)
    expected = oracle(trades, up, down)
    actual = (LEVELS.index(state.level), state.consecutive_wins, state.consecutive_losses)
    assert actual == expected
    assert state.level in LEVELS


@given(trade_lists, st.integers(0, 2))
def test_ladder_move_resets_counters(trades: list, extra: int) -> None:
    """Whenever a trade changes the level, both counters are zero afterwards."""
    state = LadderState()
    for t in trades:
        after = apply_trade(state, t, SEED)
        if after.level != state.level:
            assert (after.consecutive_wins, after.consecutive_losses) == (0, 0)
        state = after


@given(st.sampled_from(LEVELS), st.integers(0, 9))
def test_level_applies(level: str, held: int) -> None:
    """The level's position limit and risk multiplier are what the entry decision uses: the
    sized risk is the allocation risk times the level multiplier, and the position limit is
    min(policy maximum, level maximum)."""
    ladder = LadderState(level)
    limit = max_positions(level, SEED)
    assert limit == min(SEED.max_open_positions, SEED.ladder[level].max_positions)

    decision = decide(
        valid_ctx(
            ladder=ladder,
            open_positions=[position(bot=BOT) for _ in range(held)],
            total_equity=Decimal(10**12),
        )
    )
    if held >= limit:
        assert decision.reason == Reason.MAX_POSITIONS
    else:
        assert decision.accepted, decision.reason
        expected_pct = D("0.0025") * risk_multiplier(level, SEED)
        assert decision.risk_pct_used == expected_pct
        assert decision.risk_amount <= D(1_000_000) * expected_pct


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


@given(st.lists(st.just(ClosedTrade(D("0"), False)), min_size=1, max_size=20))
def test_breakeven_counts_as_a_loss(flat: list) -> None:
    """A trade closed at 0 R is a loss for the ladder (the conservative reading)."""
    state = ladder_from_trades(flat, SEED)
    assert state.consecutive_wins == 0
    assert state.level == "L1"
