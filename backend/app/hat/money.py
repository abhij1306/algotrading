"""Money helpers: Decimal only, never float (HAT-SCH-014).

HAT owns its exact-arithmetic helpers (K1, backend review C1). `to_money` rounds to paise (INR);
the column type is `numeric(18, 4)`, so storage keeps a little more.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = ["MONEY_PRECISION", "MONEY_SCALE", "PAISE", "ZERO", "Decimal", "to_decimal", "to_money"]

MONEY_PRECISION = 18
MONEY_SCALE = 4
PAISE = Decimal("0.01")
ZERO = Decimal("0")


def to_decimal(value: object, default: Decimal = ZERO) -> Decimal:
    """`value` as a Decimal, or `default` when it is missing or not a finite number."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            # str() first: Decimal(0.1) is the binary float's exact expansion.
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return default
    return result if result.is_finite() else default


def to_money(value: Decimal) -> Decimal:
    """Quantize to paise, rounding half up."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)
