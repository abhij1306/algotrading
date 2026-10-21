"""Position size (06 §2, HAT-RISK-020/021/022), in Decimal, with an exact floor.

    q = floor( min( E*r/(P_entry - P_stop),  E*p_max/P_entry,  adv_cap_pct*ADV20,  C/P_entry ) )

`r` is the sleeve's allocation `risk_pct` (which already holds the regime and health multipliers,
HAT-PORT-052) times the exposure-ladder risk multiplier. No multiplier is applied a second time
here. Each term is floored with exact integer division, so a term that is a hair under a whole
number never rounds up across a cap. The smallest floored term is the binding one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Final

from ..errors import HatError

# The terms in tie-break order: when two floors are equal the first one named binds.
TERMS: Final = ("risk", "position", "liquidity", "cash")
UNBOUNDED_CASH: Final = Decimal("Infinity")
_PRECISION = 120  # wide enough that no product or quotient of a plausible input is rounded


class SizingInputError(HatError):
    """An input is outside its domain (a negative equity, a fraction above 1)."""


class MissingInputError(SizingInputError):
    """An input is missing (None), not exact (a float or a bool) or not finite."""


@dataclass(frozen=True)
class SizingInputs:
    equity: Decimal  # E: the sleeve's equity, > 0
    allocation_risk_pct: Decimal  # hat_allocation_daily.risk_pct, in (0, 1]
    level_risk_mult: Decimal  # exposure-ladder multiplier, in (0, 1]
    entry_price: Decimal
    stop_price: Decimal
    p_max: Decimal  # max position share of the sleeve, in (0, 1]
    adv20: Decimal  # 20-day average daily volume in shares, >= 0
    adv_cap_pct: Decimal  # the policy's liquidity cap, in (0, 1]
    free_cash: Decimal  # C: free cash at the broker, >= 0 (Infinity means unbounded)


@dataclass(frozen=True)
class SizingResult:
    quantity: int  # 0 when the signal is skipped
    binding: str  # which term set the size: one of TERMS
    risk_pct_used: Decimal  # allocation risk_pct x ladder multiplier
    terms: dict[str, int | None]  # each term floored; None for an unbounded cash term
    reason: str | None  # None when quantity >= 1, else why the trade is skipped


def exact(value: object, name: str, *, cash: bool = False) -> Decimal:
    """`value` as a finite Decimal; a float, bool, None or NaN raises `MissingInputError`.
    Only the free-cash input may be +Infinity (unbounded)."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise MissingInputError(f"{name}: not an exact number")
    result = Decimal(value)
    if cash and result == UNBOUNDED_CASH:
        return result
    if not result.is_finite():
        raise MissingInputError(f"{name}: not finite")
    return result


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
    return int(numerator // denominator)


def size_position(inputs: SizingInputs) -> SizingResult:
    """The quantity for a long entry, or a skip with the reason.

    Raises `SizingInputError` for a missing, float, non-finite or out-of-domain number. A stop at
    or above the entry (or not above zero) is a skip, `stop_not_below_entry`, with quantity 0.
    """
    equity = exact(inputs.equity, "equity")
    alloc = exact(inputs.allocation_risk_pct, "allocation_risk_pct")
    level = exact(inputs.level_risk_mult, "level_risk_mult")
    entry = exact(inputs.entry_price, "entry_price")
    stop = exact(inputs.stop_price, "stop_price")
    p_max = exact(inputs.p_max, "p_max")
    adv20 = exact(inputs.adv20, "adv20")
    adv_cap = exact(inputs.adv_cap_pct, "adv_cap_pct")
    cash = exact(inputs.free_cash, "free_cash", cash=True)
    one = Decimal(1)
    if equity <= 0 or entry <= 0 or adv20 < 0 or cash < 0:
        raise SizingInputError("equity and entry must be positive, volume and cash not negative")
    if not (0 < alloc <= one and 0 < level <= one and 0 < p_max <= one and 0 < adv_cap <= one):
        raise SizingInputError("a risk, share or cap fraction is outside (0, 1]")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        risk_pct = alloc * level
        if stop <= 0 or stop >= entry:
            return SizingResult(0, "risk", risk_pct, dict.fromkeys(TERMS), "stop_not_below_entry")
        terms: dict[str, int | None] = {
            "risk": _floor_div(equity * risk_pct, entry - stop),
            "position": _floor_div(equity * p_max, entry),
            "liquidity": int((adv_cap * adv20).to_integral_value(rounding=ROUND_FLOOR)),
            "cash": None if cash == UNBOUNDED_CASH else _floor_div(cash, entry),
        }
    bounded = {name: value for name, value in terms.items() if value is not None}
    quantity = min(bounded.values())
    binding = next(name for name in TERMS if bounded.get(name) == quantity)
    reason = None if quantity >= 1 else f"size_zero_{binding}"
    return SizingResult(quantity, binding, risk_pct, terms, reason)
