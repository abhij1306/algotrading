"""The policy file's text (`risk.policy_file`) and the pre-trading reset's refusal predicate
(`risk.policy.pre_trading`). Small examples; no database."""

from __future__ import annotations

import pytest

from app.hat.risk.policy import PolicyError, pre_trading, seed_policy
from app.hat.risk.policy_file import policy_text, policy_yaml


def test_policy_yaml_reads_back_to_the_same_policy() -> None:
    policy = seed_policy()
    text = policy_yaml(policy, "risk policy version 2, in force")
    assert text.startswith("# risk policy version 2, in force\n")
    assert policy_text(text) == policy
    with pytest.raises(PolicyError):
        policy_text(text.replace("max_entries_per_day: 3\n", ""))


@pytest.mark.parametrize(
    ("orders", "positions", "trades", "allowed"),
    [
        (0, 0, 0, True),
        (1, 0, 0, False),
        (0, 1, 0, False),
        (0, 0, 1, False),
        (3, 2, 1, False),
    ],
)
def test_pre_trading_only_with_no_real_order_position_or_trade(
    orders: int, positions: int, trades: int, allowed: bool
) -> None:
    assert pre_trading(orders, positions, trades) is allowed
