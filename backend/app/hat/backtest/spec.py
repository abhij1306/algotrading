"""What a backtest run is (HAT-BT-010, HAT-BT-070): its spec, its conventions and the fixed facts
every run records. A leaf of the backtest package: the loop, the report and the evidence read
it, and it imports none of them.

A run is one strategy version under one risk-policy version over a period, with a fill model
(slippage and cost model, `costs`) and its conventions (`RunConventions`, PR 62, O5): the entry
fill rule, the slot order, the ambiguous-day rule, the bar-less sessions before the suspension
markdown, the sleeve weight and the regime rule. A run flagged for promotion (`for_promotion`,
the only kind `publish_version` accepts and the sleeve's evidence reads) is refused under
conventions live does not trade by. Every run records its conventions and hashes them into its
`config_digest`, so two runs under different conventions never read as comparable.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..costs import BacktestFills
from ..engine.portfolio import regime as regime_v1
from ..engine.portfolio.allocator import sleeve_key
from ..engine.scan import EP_PLAYBOOK
from ..engine.strategy.config import StrategyConfig
from ..errors import HatError
from ..risk.policy import PolicyError, PolicyVersion, parse_policy
from .fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    FILL_PATH,
    GAP_THROUGH_RULE,
    PENETRATION,
    SUSPENSION_BAND_PCT,
    SUSPENSION_SESSIONS,
    TOUCH,
    TRIGGER_PROXY,
)
from .slots import SLOT_ORDER_TIME, is_slot_order

__all__ = [
    "ENTRY_DAY_LOW",
    "GAP_PLAYBOOKS",
    "INITIAL_EQUITY",
    "KNOWN_GAPS",
    "LOWER_CIRCUIT_RULE",
    "REGIME_MULT_ONE",
    "REGIME_MULT_POLICY",
    "REGIME_MULT_RULES",
    "TOUCH_GAP",
    "BacktestError",
    "RunConventions",
    "RunSpec",
    "known_gaps",
    "sleeve_weight",
    "with_cooldown",
]

INITIAL_EQUITY = Decimal(1_000_000)
# What the daily-bar loop does not model, by fixed code (HAT-BT-070); measured in shadow.
KNOWN_GAPS = (
    "trigger_is_level_break_only",  # volume_pace and the opening range need intraday bars
    "episodic_pivot_not_backtested",
    "close_exits_at_official_close",
    "kill_switches_not_simulated",
    "gap_over_fills_at_open",  # live fills at the first tick after the open
    "upper_circuit_inferred_from_bar",  # O=H=C at an NSE band (lower: O=H=L=C, PR 64)
    "multipliers_fixed_at_one",  # health is 1; regime too unless `regime_mult` is `policy`
    "entry_day_low_estimated",  # without bars_5m the pre-break low is the adr20 estimate
    # the NIFTY 500 membership is today's list over the whole period, not point in time: a
    # survivorship bias accepted until a constituents history exists (QA5)
    "universe_from_current_constituents",
)
# The playbooks a known gap names: the lifecycle gate refuses a version that enables one of them
# on this evidence (PR 69, HAT-PORT-061). A gap not listed here names no playbook: it holds for
# all of them alike, a measured approximation of the daily-bar loop, and blocks none.
GAP_PLAYBOOKS: dict[str, tuple[str, ...]] = {"episodic_pivot_not_backtested": (EP_PLAYBOOK,)}
# Reported, besides `KNOWN_GAPS`, by a run under the `touch` entry rule (PR 62).
TOUCH_GAP = "entry_fills_on_touch"  # a high at the level fills; live needs a trade above it
# A stop on a session pinned at its lower band waits for the next open with a bar not pinned.
LOWER_CIRCUIT_RULE = "carry_to_next_unlocked_open"
MAX_SUSPENSION_SESSIONS = 250  # about a year of sessions
# The regime multiplier a run sizes with (PR 68, HAT-BT-014): 1, or the live regime engine's.
REGIME_MULT_ONE = "1"
REGIME_MULT_POLICY = "policy"
REGIME_MULT_RULES = (REGIME_MULT_ONE, REGIME_MULT_POLICY)
# How the loop reads the entry day's low before and after the break (HAT-BT-020, PR 60).
ENTRY_DAY_LOW = "bars_5m_else_max_low_min_open_level_less_adr20"


class BacktestError(HatError):
    """A run was refused, or failed (`backtest_error`); `code` is a fixed word and carries no
    data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunConventions:
    """The rules a run chooses besides its slippage and cost model (PR 62, O5): the entry fill
    rule (HAT-BT-020), the slot order (HAT-BT-026, PR 63), the ambiguous-day rule (HAT-BT-021)
    and the bar-less sessions before the suspension markdown (PR 64), the sleeve weight (None:
    the policy's base weight) and the regime rule the entries are sized under (HAT-BT-014,
    PR 68). Refused at construction (`BacktestError`): an unknown entry fill rule, slot order,
    ambiguous-day rule or regime rule, a bar-less session count that is not a whole number in
    0..`MAX_SUSPENSION_SESSIONS`, and a sleeve weight that is not a Decimal in (0, 1]."""

    entry_fill_rule: str = PENETRATION  # `touch` is the variant
    slot_order: str = SLOT_ORDER_TIME  # `score` and `random:K` are variants
    ambiguous: str = AMBIGUOUS_OPTIMISTIC  # `pessimistic` fills a raised stop's dip
    suspension_sessions: int = SUSPENSION_SESSIONS
    sleeve_weight: Decimal | None = None
    regime_mult: str = REGIME_MULT_ONE  # `policy` applies the live regime engine

    def __post_init__(self) -> None:
        if self.entry_fill_rule not in ENTRY_FILL_RULES:
            raise BacktestError("backtest_bad_fill_rule")
        if not is_slot_order(self.slot_order):
            raise BacktestError("backtest_bad_slot_order")
        if self.ambiguous not in AMBIGUOUS_RULES:
            raise BacktestError("backtest_bad_ambiguous_rule")
        after = self.suspension_sessions
        if (
            isinstance(after, bool)
            or not isinstance(after, int)
            or not 0 <= after <= MAX_SUSPENSION_SESSIONS
        ):
            raise BacktestError("backtest_bad_suspension")
        weight = self.sleeve_weight
        if weight is not None and not (
            isinstance(weight, Decimal) and weight.is_finite() and 0 < weight <= 1
        ):
            raise BacktestError("backtest_bad_sleeve_weight")
        if self.regime_mult not in REGIME_MULT_RULES:
            raise BacktestError("backtest_bad_regime_mult")

    def check_promotion(self, slippage_bps: Decimal) -> None:
        """Refuse these conventions for a run flagged for promotion: perfect fills (a slippage
        of 0), the optimistic `touch` rule, a slot order other than the estimated trigger time,
        a suspension markdown later than `SUSPENSION_SESSIONS`, a weight other than the policy's
        base weight (PR 68) or a regime rule other than `policy` (PR 69)."""
        if slippage_bps == 0:
            raise BacktestError("backtest_promotion_without_slippage")
        if self.entry_fill_rule != PENETRATION:
            raise BacktestError("backtest_promotion_on_touch")
        if self.slot_order != SLOT_ORDER_TIME:
            raise BacktestError("backtest_promotion_off_time_order")
        if self.suspension_sessions > SUSPENSION_SESSIONS:
            raise BacktestError("backtest_promotion_late_suspension")
        if self.sleeve_weight is not None:
            raise BacktestError("backtest_promotion_off_base_weight")
        if self.regime_mult != REGIME_MULT_POLICY:  # live sizes under the regime engine (PR 69)
            raise BacktestError("backtest_promotion_regime_variant")

    def record(
        self, fills: BacktestFills, weight: Decimal, table: Mapping[str, Decimal]
    ) -> dict[str, Any]:
        """The conventions a run used, as recorded in its metrics and hashed into its
        `config_digest`, with the run's fills, its resolved sleeve weight and the regime table it
        sized under. The keys and their order are the stored format: a change makes new digests
        differ from every stored run's."""
        return {
            "fill_path": FILL_PATH,
            "entry_fill_rule": self.entry_fill_rule,
            "gap_through_rule": GAP_THROUGH_RULE,
            "trigger_proxy": TRIGGER_PROXY,
            # normalised, so 20 and 20.0 bps record (and hash) alike
            "slippage_bps": format(abs(fills.slippage_bps).normalize(), "f"),  # -0 is 0
            "cost_model": fills.model.to_json(),
            "slot_order": self.slot_order,
            "ambiguous": self.ambiguous,
            "lower_circuit": LOWER_CIRCUIT_RULE,
            "suspension_markdown": {
                "after_sessions": self.suspension_sessions,
                "band_pct": str(SUSPENSION_BAND_PCT),
            },
            # the sizing; the regime table only when it is applied
            "sleeve_weight": format(weight.normalize(), "f"),
            "regime_mult": self.regime_mult,
            "health_mult": "1",
            **(
                {"regime_table": {k: str(table[k]) for k in regime_v1.STATES}}
                if self.regime_mult == REGIME_MULT_POLICY
                else {}
            ),
        }


@dataclass(frozen=True)
class RunSpec:
    """One run: a strategy version, one risk-policy version, a period and a fill model."""

    config: StrategyConfig
    strategy_version_id: uuid.UUID
    policy: PolicyVersion
    start: date
    end: date
    fills: BacktestFills
    initial_equity: Decimal = INITIAL_EQUITY
    for_promotion: bool = False  # the run may evidence a version (`publish_version`)
    conventions: RunConventions = field(default_factory=RunConventions)
    # the regime multiplier table the `policy` rule sizes under: the stored one live allocates
    # with, read when the run starts (C23 E3, `regime_service.stored_regime_table`)
    regime_table: Mapping[str, Decimal] = field(
        default_factory=lambda: dict(regime_v1.DEFAULT_MULTIPLIERS), hash=False
    )

    def __post_init__(self) -> None:
        """Refuse a run flagged for promotion under conventions it may not use, a sleeve weight
        the policy cannot give (`sleeve_weight`) and a regime table without an exact multiplier
        for every state: a spec that exists is a valid run."""
        if self.for_promotion:
            self.conventions.check_promotion(self.fills.slippage_bps)
        sleeve_weight(self)
        if set(self.regime_table) != set(regime_v1.STATES) or not all(
            isinstance(v, Decimal) and v.is_finite() for v in self.regime_table.values()
        ):
            raise BacktestError("backtest_bad_regime_table")

    def recorded_conventions(self) -> dict[str, Any]:
        """The conventions block recorded in the run's metrics, which `config_digest` hashes."""
        return self.conventions.record(self.fills, sleeve_weight(self), self.regime_table)


def sleeve_weight(spec: RunSpec) -> Decimal:
    """The run's sleeve weight (HAT-BT-014): its conventions' `sleeve_weight`, else the
    policy's base weight of the strategy's sleeve; `backtest_no_base_weight` when the policy
    has none or gives it 0 (a policy may), and `backtest_weight_above_cluster_cap` above the
    policy's cluster cap, which the allocator would silently cut it to."""
    policy = spec.policy.policy
    weight = spec.conventions.sleeve_weight
    if weight is None:
        weight = policy.base_weight.get(sleeve_key(spec.config.bot))
        if weight is None or weight <= 0:
            raise BacktestError("backtest_no_base_weight")
    if weight > policy.cluster_cap:
        raise BacktestError("backtest_weight_above_cluster_cap")
    return weight


def known_gaps(spec: RunSpec) -> list[str]:
    """The run's known gaps (HAT-BT-070): `KNOWN_GAPS`, and the touch fill under `touch`."""
    return [*KNOWN_GAPS, *([TOUCH_GAP] if spec.conventions.entry_fill_rule == TOUCH else [])]


def with_cooldown(policy: PolicyVersion, sessions: int) -> PolicyVersion:
    """`policy` with its `cooldown_sessions` set to `sessions`, the backtest's sweep of the
    per-instrument cooldown (HAT-RISK-036, PR 58). The run records the value it used; a value
    the policy schema refuses raises `BacktestError`."""
    try:
        changed = parse_policy({**policy.policy.to_json(), "cooldown_sessions": sessions})
    except PolicyError:
        raise BacktestError("backtest_bad_cooldown") from None
    return PolicyVersion(
        policy.version_id, policy.version, policy.effective_from, policy.superseded_at, changed
    )
