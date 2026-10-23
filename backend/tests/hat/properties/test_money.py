"""PBT money.exact (HAT-SCH-014; PR 75c, architecture A-F7): the one Decimal boundary."""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

import numpy as np
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.money import NotExactError, exact, require_exact

_TEXT = st.one_of(
    st.decimals(allow_nan=True, allow_infinity=True, places=None).map(str),
    st.sampled_from(
        [
            "1e999",
            "1e99999999999999999999",
            " 1.5 ",
            "1,234.50",
            "1,5",
            "1_5",
            "١٢",
            "１２",
            "NaN",
            "sNaN",
            "-Infinity",
            "",
            ".",
        ]
    ),
    st.text(alphabet="0123456789.,-+eE ", max_size=8),
)
VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(allow_nan=True, allow_infinity=True).map(np.float64),
    st.integers(-(2**62), 2**62).map(np.int64),
    _TEXT,
    st.just(object()),
)


@given(
    value=VALUES,
    floats=st.booleans(),
    positive=st.booleans(),
    places=st.none() | st.integers(0, 6),
    grouped=st.booleans(),
)
@example(value=0.1, floats=True, positive=False, places=None, grouped=False)
@example(value=np.float64(0.1), floats=True, positive=False, places=None, grouped=False)
@example(value="1e999", floats=False, positive=False, places=4, grouped=False)
@example(value="1e99999999999999999999", floats=False, positive=False, places=None, grouped=False)
@example(value="0.125", floats=False, positive=False, places=2, grouped=False)  # a tie: 0.12
@example(value="1,5", floats=False, positive=False, places=None, grouped=False)
@example(value="1_5", floats=False, positive=False, places=None, grouped=True)
@example(value=0.00004, floats=True, positive=True, places=4, grouped=False)
def test_exact(
    value: object, floats: bool, positive: bool, places: int | None, grouped: bool
) -> None:
    """`exact` is the one way HAT reads an untrusted number: the answer is None or a finite
    Decimal; None, a bool, NaN, an infinity and any other object are never a number; a float
    (numpy's too) is refused unless `floats`, and is then read by its shortest text, so 0.1 is
    exactly 0.1; text is read as written, in ASCII and without underscores (a comma only with
    `grouped`; never "1_5" or another script's digits); `places` rounds half-even
    to exactly that many places, and `positive` refuses what is (or rounds to) zero or less.
    `require_exact` raises `NotExactError` (an `InvalidOperation` and a `ValueError`) exactly
    when `exact` answers None. Harm: two readers of one tick or reply disagree, a float's binary
    expansion or a NaN reaches money, or "1,5" is read as 15."""
    got = exact(value, floats=floats, positive=positive, places=places, grouped=grouped)
    try:
        required = require_exact(
            value, "v", floats=floats, positive=positive, places=places, grouped=grouped
        )
    except NotExactError as exc:
        assert isinstance(exc, InvalidOperation)
        assert isinstance(exc, ValueError)
        assert got is None
    else:
        assert got is not None
        assert required == got
    if got is None:
        _assert_refused(value, floats=floats, positive=positive, places=places, grouped=grouped)
        return
    assert isinstance(got, Decimal)
    assert got.is_finite()
    assert not isinstance(value, bool)
    assert value is not None
    read = _read_as(value, grouped=grouped)
    if isinstance(value, float | np.floating):
        assert floats
    if places is None:
        assert got == read
    else:
        assert got.as_tuple().exponent == -places
        assert got == read.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN)
    if positive:
        assert got > 0


def _read_as(value: object, *, grouped: bool) -> Decimal:
    """What a number names, read independently of `exact`."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | np.integer):
        return Decimal(int(value))
    if isinstance(value, float | np.floating):
        return Decimal(repr(float(value)))  # the shortest text that round-trips
    assert isinstance(value, str)
    text = (value.replace(",", "") if grouped else value).strip()
    if not text.isascii() or "_" in text:  # Decimal alone also reads "1_5" and other digits
        raise InvalidOperation(text)
    return Decimal(text)


def _assert_refused(
    value: object, *, floats: bool, positive: bool, places: int | None, grouped: bool
) -> None:
    """`exact` said None: the value is not a number, or an option refused it."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float | np.floating) and not floats:
        return
    if not isinstance(value, Decimal | int | np.integer | float | np.floating | str):
        return
    try:
        read = _read_as(value, grouped=grouped)
    except InvalidOperation:
        return  # text Decimal cannot read
    if not read.is_finite():
        return
    if places is not None:
        try:
            read = read.quantize(Decimal(1).scaleb(-places))
        except InvalidOperation:
            return  # too large for that many places
    assert positive, (value, read)
    assert read <= 0, (value, read)


@pytest.mark.parametrize("float_type", [float, np.float64])
@given(number=st.floats(allow_nan=False, allow_infinity=False))
def test_float_text(float_type: type, number: float) -> None:
    """PBT money.float_text: a Python float and a numpy float of the same value read
    as the same Decimal, the one the float's shortest text names. Harm: `Decimal(repr(x))` of a
    numpy float raises (its repr is "np.float64(0.1)") where `str` reads it, so two copies of
    one reader disagreed."""
    assert exact(float_type(number), floats=True) == Decimal(repr(number))
