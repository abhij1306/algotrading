"""Money and the one Decimal boundary: Decimal only, never float.

Every number HAT reads from outside (a feed tick, a broker reply, a Parquet column, a CSV cell,
a config value, a command argument) becomes exact here, by one rule (`exact`):

- None, a bool, NaN, an infinity, text Decimal cannot read and any other object is not a number.
- Text is read as written, surrounding spaces ignored, in ASCII and without underscores (Python's
  own reading would also take "1_5" as 15, and other scripts' digits); an exponent too large for
  Decimal is not a number either. Only a reader of an NSE format that groups digits
  ("1,234.50") passes `grouped=True` to drop the commas, so "1,5" is never read as 15 elsewhere.
- A float is refused unless the caller reads a float source (`floats=True`: Parquet, polars, a
  JSON reply). It is read by its shortest text (`str`), so 0.1 is Decimal("0.1"), never the
  binary expansion, and a numpy float64 reads the same as a Python float of its value.
- `places` quantizes half-even (a value too large for that many places is not a number);
  `positive` then refuses zero and negatives, so a price that rounds to 0 is refused.

HAT owns its exact-arithmetic helpers (K1, backend review C1). `to_money` rounds to paise (INR)
half up; the column type is `numeric(18, 4)`, so storage keeps a little more.
"""

from __future__ import annotations

import numbers
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = [
    "MONEY_PRECISION",
    "MONEY_SCALE",
    "PAISE",
    "ZERO",
    "Decimal",
    "NotExactError",
    "exact",
    "require_exact",
    "to_money",
]

MONEY_PRECISION = 18
MONEY_SCALE = 4
PAISE = Decimal("0.01")
ZERO = Decimal("0")


class NotExactError(InvalidOperation, ValueError):
    """A value `require_exact` refused (an `InvalidOperation` and a `ValueError`, so a reader that
    already treats either as unreadable keeps doing so)."""


def exact(
    value: object,
    *,
    floats: bool = False,
    positive: bool = False,
    places: int | None = None,
    grouped: bool = False,
) -> Decimal | None:
    """`value` as a finite Decimal by the module's rule, or None when it is not one."""
    if grouped and isinstance(value, str):
        value = value.replace(",", "")
    number = _read(value, floats=floats)
    if number is None or not number.is_finite():
        return None
    if places is not None:
        try:
            number = number.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN)
        except InvalidOperation:  # too large to hold at `places`
            return None
    if positive and number <= 0:
        return None
    return number


def require_exact(
    value: object,
    name: str,
    *,
    floats: bool = False,
    positive: bool = False,
    places: int | None = None,
    grouped: bool = False,
) -> Decimal:
    """`exact(value, ...)`, raising `NotExactError` naming `name` when it is not a number."""
    number = exact(value, floats=floats, positive=positive, places=places, grouped=grouped)
    if number is None:
        raise NotExactError(f"{name}: not an exact number")
    return number


def to_money(value: Decimal) -> Decimal:
    """Quantize to paise, rounding half up."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def _read(value: object, *, floats: bool) -> Decimal | None:
    kind = type(value)
    if kind is Decimal:  # the common cases first: this runs per bar in the criteria
        return value  # type: ignore[return-value]
    if kind is float:
        return Decimal(str(value)) if floats else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, numbers.Integral):  # int, and numpy's integers
        return Decimal(int(value))
    if isinstance(value, str):
        text = value.strip()
        return _read_text(text) if text.isascii() and "_" not in text else None
    if isinstance(value, numbers.Real) and floats:
        return _read_text(str(value))  # a Real whose text is not a number ("1/3") is None
    return None


def _read_text(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:  # not a number, or an exponent Decimal cannot hold
        return None
