"""The nightly allocator (05 §5, HAT-PORT-050..056) as a pure function.

    w_s = b_s x m_regime_s x m_health_s,   m in [lo, hi] from the risk policy

A sleeve that is not Live has weight 0. Each multiplier moves at most `SMOOTHING_STEP` a day from
its previous stored value. Two caps then hold the weights: the risk cluster cap (the sum of a
cluster's final weights is at most the policy's `cluster_cap`) and the whole book (the sum is at
most 1). A cap only ever scales weights down, in proportion, rounded down, and nothing removed
from one sleeve is given to another: what is left is cash. Money is `Decimal` throughout.

When a cap binds, `final_weight` is below `base x regime_mult x health_mult`; otherwise the
product holds exactly (row by row, HAT-PORT-050).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import UUID

from app.hat.errors import HatError
from app.hat.risk.policy import RiskPolicy

from .health import Health

SMOOTHING_STEP = Decimal("0.25")  # [A] HAT-PORT-054
LIVE = "live"
_PRECISION = 120  # wide enough that no product of stored amounts is rounded
_WEIGHT_PLACES = Decimal("1E-12")
_MONEY_PLACES = Decimal("0.0001")
# Sleeve keys of the two MVP strategies in the policy's `base_weight` (06 §1). A strategy not
# listed is its own key, so a new sleeve needs a policy entry and no code (HAT-PORT-010).
SLEEVE_OF_STRATEGY: Mapping[str, str] = {
    "stage2-contraction-breakout": "S1",
    "momentum-breakout-ep": "S2",
}


class AllocationError(HatError):
    """An allocation input is unusable; the job fails and no weight is made on a guess."""


def sleeve_key(strategy_name: str) -> str:
    """The `base_weight` key of a sleeve's strategy."""
    return SLEEVE_OF_STRATEGY.get(strategy_name, strategy_name)


def clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return min(max(value, lo), hi)


def smooth(previous: Decimal | None, target: Decimal, step: Decimal = SMOOTHING_STEP) -> Decimal:
    """`target`, or the value `step` closer to it than `previous` when it is further than that.
    With no previous value the multiplier starts at its target."""
    if previous is None:
        return target
    return previous + min(max(target - previous, -step), step)


@dataclass(frozen=True)
class SleeveInput:
    """What the allocator reads of one sleeve (`hat_bot`) for a trade date."""

    bot_id: UUID
    sleeve: str  # the `base_weight` key
    cluster: str  # `hat_bot.risk_cluster`
    lifecycle_state: str
    regime_table_value: Decimal  # the confirmed state's multiplier for this sleeve
    health: Health  # from the sleeve's latest snapshot; the full value when it has none
    previous_regime_mult: Decimal | None = None
    previous_health_mult: Decimal | None = None


@dataclass(frozen=True)
class Allocation:
    """One `hat_allocation_daily` row's numbers."""

    bot_id: UUID
    base_weight: Decimal
    regime_mult: Decimal
    health_mult: Decimal
    final_weight: Decimal
    capital: Decimal
    risk_pct: Decimal


def _scale_down(weights: list[Decimal], limit: Decimal) -> list[Decimal]:
    """`weights` with their sum held to `limit`: proportional, rounded down (so the sum never
    exceeds it) and never raised. Unchanged when the sum is already within it."""
    total = sum(weights, Decimal(0))
    if total <= limit:
        return weights
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return [(w * limit / total).quantize(_WEIGHT_PLACES, rounding=ROUND_DOWN) for w in weights]


def allocate(
    sleeves: Sequence[SleeveInput], policy: RiskPolicy, total_equity: Decimal
) -> list[Allocation]:
    """One allocation per sleeve, in the order given.

    Bounds, base weights, the cluster cap and the risk per trade are the policy's. Raises
    `AllocationError` for a non-Decimal or negative total equity.
    """
    if not isinstance(total_equity, Decimal) or not total_equity.is_finite() or total_equity < 0:
        raise AllocationError("allocation_bad_equity")
    lo, hi = policy.multiplier_lo, policy.multiplier_hi
    rows: list[tuple[SleeveInput, Decimal, Decimal, Decimal, Decimal]] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for s in sleeves:
            base = policy.base_weight.get(s.sleeve, Decimal(0))
            regime = clamp(
                smooth(s.previous_regime_mult, clamp(s.regime_table_value, lo, hi)), lo, hi
            )
            if s.lifecycle_state == LIVE and not s.health.probation:
                # a previous 0 is a sleeve that was out of Live: it restarts at its target
                previous = s.previous_health_mult if s.previous_health_mult else None
                health = clamp(smooth(previous, clamp(s.health.multiplier, lo, hi)), lo, hi)
            else:
                health = Decimal(0)
            rows.append((s, base, regime, health, base * regime * health))
    weights = [r[4] for r in rows]
    for cluster in {r[0].cluster for r in rows}:
        members = [i for i, r in enumerate(rows) if r[0].cluster == cluster]
        scaled = _scale_down([weights[i] for i in members], policy.cluster_cap)
        for i, w in zip(members, scaled, strict=True):
            weights[i] = w
    weights = _scale_down(weights, Decimal(1))
    out = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for (s, base, regime, health, _), final in zip(rows, weights, strict=True):
            out.append(
                Allocation(
                    bot_id=s.bot_id,
                    base_weight=base,
                    regime_mult=regime,
                    health_mult=health,
                    final_weight=final,
                    capital=(final * total_equity).quantize(_MONEY_PLACES, rounding=ROUND_DOWN),
                    risk_pct=policy.risk_pct * regime * health,
                )
            )
    return out
