"""GTT stop pricing (HAT-EXEC-062; 07 §6 step 3).

The limit price of a protective GTT stop sits 1% [A] below its trigger, rounded down to the tick,
so a moderate gap-down still fills. The offset reduces the chance of an unfilled stop but cannot
remove it; a triggered GTT that does not fill raises a health alert (PR 15).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from ..errors import HatError

__all__ = ["GTT_LIMIT_OFFSET", "GttPriceError", "gtt_limit_price"]

GTT_LIMIT_OFFSET = Decimal("0.01")  # [A] 1% below the trigger


class GttPriceError(HatError):
    """No valid GTT limit price exists for this trigger and tick (fail closed)."""


def gtt_limit_price(trigger_price: Decimal, tick_size: Decimal) -> Decimal:
    """trigger x (1 - 1%), rounded down to the tick. Raises GttPriceError for a trigger or
    tick that is not a positive finite number, or when the rounded price is not positive (a
    trigger within about one tick of zero)."""
    for value in (trigger_price, tick_size):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise GttPriceError("trigger and tick must be positive finite decimals")
    raw = trigger_price * (Decimal(1) - GTT_LIMIT_OFFSET)
    limit = (raw / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
    if limit <= 0:
        raise GttPriceError("no positive limit price below this trigger")
    return limit
