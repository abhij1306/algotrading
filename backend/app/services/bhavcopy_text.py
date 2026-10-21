"""Text helpers shared by the phase-1 pipeline and HAT's bhavcopy and corporate-action loaders.

Pure functions, no I/O and no imports from the app, so `data_platform/pipelines/phase1_build.py`
(which imports it as `backend.app...`) and `app.hat` (as `app...`) can both use it. Phase 1 keeps
its own factor convention (divide older prices by a factor above 1); HAT's convention is the
reciprocal (multiply older prices by a factor in (0, 1]); both come from the same parsed ratios.
"""

from __future__ import annotations

# The text helpers live in `app.hat.data.ratios` (K1); relative so that `backend.app...` and
# `app...` both resolve. Delete with the legacy code (K5).
from ..hat.data.ratios import find_bonus_ratio, split_pair_ints, split_pair_numbers

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


__all__ = [
    "BHAVCOPY_OHLCV_COLUMNS",
    "find_bonus_ratio",
    "split_pair_ints",
    "split_pair_numbers",
]
