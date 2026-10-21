"""Text helpers shared by the phase-1 pipeline and HAT's bhavcopy and corporate-action loaders.

Pure functions, no I/O and no imports from the app, so `data_platform/pipelines/phase1_build.py`
(which imports it as `backend.app...`) and `app.hat` (as `app...`) can both use it. Phase 1 keeps
its own factor convention (divide older prices by a factor above 1); HAT's convention is the
reciprocal (multiply older prices by a factor in (0, 1]); both come from the same parsed ratios.
"""

from __future__ import annotations

import re
from fractions import Fraction

# Column knowledge of the NSE `sec_bhavdata_full` file that phase 1 reads (HAT adds the cm
# bhavcopy and UDiFF layouts in app/hat/data/bhavcopy.py).
BHAVCOPY_OHLCV_COLUMNS = {
    "SYMBOL",
    "SERIES",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "TTL_TRD_QNTY",
}


def _digit_run_end(text: str, i: int) -> int:
    while i < len(text) and text[i].isdecimal():
        i += 1
    return i


def _space_run_end(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _find_ratio(text: str, pos: int) -> tuple[int, int, int] | None:
    """(start, a, b) of the first "a:b" (spaces allowed around ":") at or after pos.

    Linear-time equivalent of re.search(r"(\\d+)\\s*:\\s*(\\d+)", text, pos).
    """
    i = pos
    while i < len(text):
        if not text[i].isdecimal():
            i += 1
            continue
        first_end = _digit_run_end(text, i)
        colon = _space_run_end(text, first_end)
        if colon < len(text) and text[colon] == ":":
            second = _space_run_end(text, colon + 1)
            second_end = _digit_run_end(text, second)
            if second_end > second:
                return i, int(text[i:first_end]), int(text[second:second_end])
        # No ratio can start inside this digit run either: skip past it.
        i = first_end
    return None


def find_bonus_ratio(text: str) -> tuple[int, int] | None:
    """First "a:b" ratio after "bonus" whose first number starts on the same line, if any."""
    pos = 0
    while (start := text.find("bonus", pos)) >= 0:
        line_end = text.find("\n", start)
        if line_end < 0:
            line_end = len(text)
        found = _find_ratio(text, start + len("bonus"))
        if found and found[0] < line_end:
            return found[1], found[2]
        pos = line_end + 1
    return None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def split_pair_ints(text: str) -> tuple[Fraction, Fraction] | None:
    """The first two numbers of a lower-cased face-value split text ("from 10 to 2" gives
    (10, 2), "from rs 10 to rs 2.5" gives (10, 5/2)); None when fewer than two or the second is
    zero."""
    nums = [Fraction(n) for n in _NUMBER.findall(text)]
    if len(nums) >= 2 and nums[1] != 0:
        return nums[0], nums[1]
    return None


def split_pair_numbers(text: str) -> tuple[Fraction, Fraction] | None:
    """The old and new face value of a split text, decimals allowed ("from Rs 10 to Rs 2.5");
    None unless there are exactly two positive numbers."""
    nums = [Fraction(n) for n in _NUMBER.findall(text)]
    if len(nums) == 2 and all(n > 0 for n in nums):
        return nums[0], nums[1]
    return None
