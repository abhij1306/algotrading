"""`book_vcp`: the S1 book's base (owner spec, sections C to G), one hard criterion whose rules
each have their own parameter and failure reason, checked in the funnel's order (`GATES`):

- geometry (C): H0 is the highest high of the last `h0_sessions`, after which a low fell at
  least `h0_decline_pct` below it (`no_base`). Swings are a zigzag on the daily highs and lows
  from H0, reversing at max(`swing_min_pct`, `swing_atr_multiple` x ATR / close) of the bar
  that confirms the reversal (`zigzag`); only swings confirmed by the signal date exist.
  Contraction i is swing high Hi to the next confirmed swing low Li, the first starting at H0,
  depth (Hi - Li) / Hi; no swing high above H0. The base (H0 to the signal date, both counted)
  lasts `base_sessions`; there are `contractions` of them; the first is `first_depth_pct` deep;
  each at most `depth_ratio_max` x the one before; the final at most `final_depth_max_pct` deep
  and at least `final_min_sessions` long. A pullback after the zigzag's last contraction too
  shallow for the zigzag to confirm is the final contraction when it is a window
  (`tight_window`): from the highest high after the last swing low through the signal date,
  `window_sessions` long, at most `final_depth_max_pct` and `depth_ratio_max` x the last zigzag
  contraction deep; without one the zigzag's last contraction is the final one. Pivot: the
  final contraction's highest high; stop basis: its lowest low.
- time compression (D): sessions from the base's deepest low to the signal date at least
  `tc_min_ratio` x the sessions from H0 to that low, and at least `tc_min_sessions`.
- dry-up (E): over the final contraction, mean volume below `dryup_mean_max` x the mean of the
  `dryup_avg_days` sessions ending on its first session, and one session below
  `dryup_session_max` x that mean.
- relative correction (F): rejected when Depth_1 > `rc_max_ratio` x the NIFTY 500's largest
  peak-to-trough fall of closes from H0's date to the signal date and Depth_1 > `rc_min_depth_pct`.
- base count (G): 1 + the earlier bases (geometry as above, a different H0) whose pivot a later
  close through the signal date went above, counted from the later of the stage-2 start (the
  first session the trend template passes after `stage2_fail_sessions` failing ones) and the last
  close below the SMA200; without a stage-2 start in the history, from the first session with a
  full template history (`stage2_start_unknown`). Rejected at `max_base_number` + 1. The
  template here is the eight price checks (`template_passes`): the RS rank of an earlier date is
  not in the history a criterion reads.

A rule whose parameter is None is off. Every value reads bars dated on or before the signal
date. Depth rules compare exact decimals; the zigzag, ATR and the history template use doubles.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from pydantic import Field, model_validator

from .base import REASON_OK, CriterionResult, EvalContext, ParamModel, dec, finite, no_data, verdict

# Funnel gates in evaluation order, each with the failure reasons that stop a stock-day there.
GEOMETRY = "vcp_geometry"
TIME_COMPRESSION = "time_compression"
DRY_UP = "dry_up"
RELATIVE_CORRECTION = "relative_correction"
BASE_COUNT = "base_count"
GATES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        GEOMETRY,
        frozenset(
            {
                "no_data",
                "no_base",
                "base_too_short",
                "base_too_long",
                "swing_high_above_h0",
                "too_few_contractions",
                "too_many_contractions",
                "first_depth_out_of_band",
                "depth_ratio_too_wide",
                "final_depth_too_deep",
                "final_contraction_too_short",
            }
        ),
    ),
    (TIME_COMPRESSION, frozenset({"time_not_compressed", "too_soon_after_low"})),
    (DRY_UP, frozenset({"no_volume_data", "no_volume_dryup", "no_dryup_session"})),
    (RELATIVE_CORRECTION, frozenset({"no_benchmark", "correction_too_deep_vs_index"})),
    (BASE_COUNT, frozenset({"late_stage_base"})),
)

# The trend template's windows (spec B, the existing criteria's defaults).
SMA_FAST, SMA_MID, SMA_SLOW = 50, 150, 200
SMA_RISING_SESSIONS = 21
WINDOW_52W = 252
LOW_52W_MIN = 1.30
HIGH_52W_MIN = 0.75
_MEMO = "book_vcp"
_HUNDRED = Decimal(100)


def gate_of(reason: str) -> str:
    """The funnel gate a failure reason of `book_vcp` stops a stock-day at (geometry when the
    reason is not one of `GATES`)."""
    for gate, reasons in GATES:
        if reason in reasons:
            return gate
    return GEOMETRY


class BookVcpParams(ParamModel):
    h0_sessions: int = Field(default=325, ge=20, le=700)
    h0_decline_pct: float = Field(default=10.0, gt=0, le=100)
    swing_min_pct: float = Field(default=3.0, gt=0, le=50)
    swing_atr_multiple: float = Field(default=1.5, ge=0, le=10)
    atr_sessions: int = Field(default=14, ge=1, le=100)
    contractions: list[int] = Field(default_factory=lambda: [2, 6], min_length=2, max_length=2)
    first_depth_pct: list[float] = Field(
        default_factory=lambda: [10.0, 35.0], min_length=2, max_length=2
    )
    depth_ratio_max: float = Field(default=0.75, gt=0, le=1)
    final_depth_max_pct: float = Field(default=10.0, gt=0, le=100)
    final_min_sessions: int = Field(default=3, ge=1, le=100)
    window_sessions: list[int] = Field(default_factory=lambda: [5, 15], min_length=2, max_length=2)
    base_sessions: list[int] = Field(default_factory=lambda: [15, 325], min_length=2, max_length=2)
    tc_min_ratio: float | None = Field(default=0.5, ge=0, le=10)
    tc_min_sessions: int | None = Field(default=10, ge=0, le=325)
    dryup_avg_days: int = Field(default=50, ge=5, le=252)
    dryup_mean_max: float | None = Field(default=0.7, gt=0, le=10)
    dryup_session_max: float | None = Field(default=0.5, gt=0, le=10)
    rc_max_ratio: float | None = Field(default=2.5, gt=0, le=100)
    rc_min_depth_pct: float = Field(default=15.0, ge=0, le=100)
    stage2_fail_sessions: int = Field(default=60, ge=1, le=500)
    max_base_number: int | None = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def _check(self) -> BookVcpParams:
        low, high = self.contractions
        if not 1 <= low <= high <= 12:
            raise ValueError("contractions is [min, max] with 1 <= min <= max <= 12")
        if not 0 <= self.first_depth_pct[0] <= self.first_depth_pct[1] <= 100:
            raise ValueError("first_depth_pct is [min, max] within 0..100")
        if not 2 <= self.base_sessions[0] <= self.base_sessions[1]:
            raise ValueError("base_sessions is [min, max] with 2 <= min <= max")
        if not 1 <= self.window_sessions[0] <= self.window_sessions[1] <= 325:
            raise ValueError("window_sessions is [min, max] with 1 <= min <= max <= 325")
        return self

    def geometry_key(self) -> tuple[object, ...]:
        """The parameters the geometry reads: an earlier date's cached geometry is reused only
        under the same ones."""
        return (
            self.h0_sessions,
            self.h0_decline_pct,
            self.swing_min_pct,
            self.swing_atr_multiple,
            self.atr_sessions,
            tuple(self.contractions),
            tuple(self.first_depth_pct),
            self.depth_ratio_max,
            self.final_depth_max_pct,
            self.final_min_sessions,
            tuple(self.window_sessions),
            tuple(self.base_sessions),
        )


# --- pure pieces ----------------------------------------------------------------------------


def swing_thresholds(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], params: BookVcpParams
) -> list[float]:
    """Each bar's zigzag reversal threshold as a fraction: max(`swing_min_pct`,
    `swing_atr_multiple` x ATR / close), the ATR the mean true range of the `atr_sessions`
    sessions ending on the bar (the session before the first gives its previous close); the
    percentage floor alone until that many sessions exist."""
    floor = params.swing_min_pct / 100
    n = len(close)
    out = [floor] * n
    length = params.atr_sessions
    if params.swing_atr_multiple == 0 or n <= length:
        return out
    ranges = [0.0] * n
    for i in range(1, n):
        prev = close[i - 1]
        ranges[i] = max(high[i], prev) - min(low[i], prev)
    total = sum(ranges[1 : length + 1])
    for i in range(length, n):
        if i > length:
            total += ranges[i] - ranges[i - length]
        if close[i] > 0:
            out[i] = max(floor, params.swing_atr_multiple * (total / length) / close[i])
    return out


@dataclass(frozen=True)
class Swing:
    high: bool  # a swing high (else a low)
    index: int
    price: float


def zigzag(
    high: Sequence[float], low: Sequence[float], threshold: Sequence[float], start: int, end: int
) -> list[Swing]:
    """The swings confirmed by bars `start..end` (inclusive), the first the swing high at
    `start`. After a high, the lowest low since is tracked; a bar whose high reaches that low x
    (1 + its threshold) confirms the low (a bar that makes a new low confirms nothing). After a
    low, symmetrically, a bar whose low reaches the highest high since x (1 - its threshold)
    confirms that high. Ties keep the earlier extreme."""
    swings = [Swing(True, start, high[start])]
    falling = True
    at, extreme = -1, 0.0
    for i in range(start + 1, end + 1):
        if falling:
            if at < 0 or low[i] < extreme:
                at, extreme = i, low[i]
            elif high[i] >= extreme * (1 + threshold[i]):
                swings.append(Swing(False, at, extreme))
                falling, at, extreme = False, i, high[i]
        elif high[i] > extreme:
            at, extreme = i, high[i]
        elif low[i] <= extreme * (1 - threshold[i]):
            swings.append(Swing(True, at, extreme))
            falling, at, extreme = True, i, low[i]
    return swings


def h0_index(
    high: Sequence[float], low: Sequence[float], end: int, params: BookVcpParams
) -> int | None:
    """The index of H0 for the signal bar `end`: the first highest high of the `h0_sessions`
    bars ending there, when a later low through `end` is at least `h0_decline_pct` below it."""
    first = max(0, end - params.h0_sessions + 1)
    window = high[first : end + 1]
    top = max(window)
    h0 = first + window.index(top)
    if h0 >= end:
        return None
    floor = dec(top) * (1 - dec(params.h0_decline_pct) / _HUNDRED)
    return h0 if dec(min(low[h0 + 1 : end + 1])) <= floor else None


@dataclass(frozen=True)
class Contraction:
    start: int  # the swing high's bar
    end: int  # the swing low's bar
    high: Decimal  # the highest high of bars start..end
    low: Decimal  # the lowest low of bars start..end

    @property
    def depth_pct(self) -> Decimal:
        return (self.high - self.low) / self.high * _HUNDRED

    @property
    def sessions(self) -> int:
        return self.end - self.start + 1


def contractions_of(
    high: Sequence[float], low: Sequence[float], swings: Sequence[Swing]
) -> list[Contraction]:
    """Each swing high with the swing low confirmed after it; a last high without one is not a
    contraction."""
    out = []
    for hi, lo in zip(swings[::2], swings[1::2], strict=False):
        a, b = hi.index, lo.index
        out.append(Contraction(a, b, dec(max(high[a : b + 1])), dec(min(low[a : b + 1]))))
    return out


def area(high: Sequence[float], low: Sequence[float], start: int, end: int) -> Contraction:
    """Bars `start..end` as a contraction: their highest high and lowest low."""
    return Contraction(start, end, dec(max(high[start : end + 1])), dec(min(low[start : end + 1])))


def tight_window(
    high: Sequence[float],
    low: Sequence[float],
    last: Contraction,
    end: int,
    sessions: Sequence[int],
    max_pct: float,
    ratio: float,
) -> Contraction | None:
    """The tight pullback after the zigzag's last confirmed contraction `last`: the bars from the
    first highest high after its swing low through `end` (so its high is the pivot), when they
    number within `sessions` and are at most `max_pct` and at most `ratio` x `last` deep; else
    None (the high within the last `sessions[0]` bars or more than `sessions[1]` back, or too
    deep). A pullback shallower than the zigzag's reversal threshold never confirms a swing, so
    the zigzag alone misses it."""
    if end <= last.end:
        return None
    after = high[last.end + 1 : end + 1]
    top = last.end + 1 + after.index(max(after))
    if not sessions[0] <= end - top + 1 <= sessions[1]:
        return None
    window = area(high, low, top, end)
    limit = min(dec(max_pct), dec(ratio) * last.depth_pct)
    return window if window.depth_pct <= limit else None


def with_window(
    high: Sequence[float],
    low: Sequence[float],
    contractions: Sequence[Contraction],
    end: int,
    params: BookVcpParams,
) -> tuple[Contraction, ...]:
    """The zigzag's contractions and, when there is one, the tight pullback after the last
    (`tight_window`) as the final contraction."""
    if not contractions:
        return tuple(contractions)
    window = tight_window(
        high,
        low,
        contractions[-1],
        end,
        params.window_sessions,
        params.final_depth_max_pct,
        params.depth_ratio_max,
    )
    return tuple(contractions) if window is None else (*contractions, window)


@dataclass(frozen=True)
class Geometry:
    h0: int
    contractions: tuple[Contraction, ...]
    swing_highs_above_h0: int

    @property
    def final(self) -> Contraction:
        return self.contractions[-1]


def geometry_fault(geo: Geometry, end: int, params: BookVcpParams) -> tuple[str, float] | None:
    """The first rule of C the base breaks, as (reason, value), or None."""
    length = end - geo.h0 + 1
    if length < params.base_sessions[0]:
        return "base_too_short", float(length)
    if length > params.base_sessions[1]:
        return "base_too_long", float(length)
    if geo.swing_highs_above_h0:
        return "swing_high_above_h0", float(geo.swing_highs_above_h0)
    count = len(geo.contractions)
    if count < params.contractions[0]:
        return "too_few_contractions", float(count)
    if count > params.contractions[1]:
        return "too_many_contractions", float(count)
    depths = [c.depth_pct for c in geo.contractions]
    low, high = (dec(v) for v in params.first_depth_pct)
    if not low <= depths[0] <= high:
        return "first_depth_out_of_band", float(depths[0])
    ratio = dec(params.depth_ratio_max)
    for before, after in zip(depths, depths[1:], strict=False):
        if after > ratio * before:
            return "depth_ratio_too_wide", float(after / before) if before else float("inf")
    if depths[-1] > dec(params.final_depth_max_pct):
        return "final_depth_too_deep", float(depths[-1])
    if geo.final.sessions < params.final_min_sessions:
        return "final_contraction_too_short", float(geo.final.sessions)
    return None


def chain_geometry(
    high: Sequence[float],
    low: Sequence[float],
    threshold: Sequence[float],
    h0: int,
    end: int,
    params: BookVcpParams,
) -> Geometry | tuple[str, float | None]:
    """The base from bar `h0` to the signal bar `end`: the zigzag's contractions from `h0` and
    the tight pullback after them (`with_window`), when it keeps every rule of C, else (reason,
    value)."""
    swings = zigzag(high, low, threshold, h0, end)
    top = high[h0]
    above = sum(1 for s in swings[1:] if s.high and s.price > top)
    chain = with_window(high, low, contractions_of(high, low, swings), end, params)
    geo = Geometry(h0, chain, above)
    fault = geometry_fault(geo, end, params)
    return geo if fault is None else fault


def base_geometry(
    high: Sequence[float],
    low: Sequence[float],
    threshold: Sequence[float],
    end: int,
    params: BookVcpParams,
) -> Geometry | tuple[str, float | None]:
    """The base of the signal bar `end` when it keeps every rule of C, else (reason, value)."""
    h0 = h0_index(high, low, end, params)
    if h0 is None:
        return "no_base", None
    return chain_geometry(high, low, threshold, h0, end, params)


def time_compression(h0: int, deepest: int, end: int) -> tuple[int, int]:
    """(sessions from H0 to the deepest low, sessions from that low to the signal bar)."""
    return deepest - h0, end - deepest


def time_fault(before: int, after: int, params: BookVcpParams) -> str | None:
    if params.tc_min_ratio is not None and dec(after) < dec(params.tc_min_ratio) * before:
        return "time_not_compressed"
    if params.tc_min_sessions is not None and after < params.tc_min_sessions:
        return "too_soon_after_low"
    return None


def dryup_ratios(
    volume: Sequence[float | None], final: Contraction, avg_days: int
) -> tuple[Decimal, Decimal] | None:
    """(mean, lowest) volume of the final contraction's sessions over the mean of the
    `avg_days` sessions ending on its first session; None without them or on a zero mean."""
    first = final.start - avg_days + 1
    if first < 0:
        return None
    window = volume[first : final.start + 1]
    inside = volume[final.start : final.end + 1]
    if any(v is None for v in (*window, *inside)):
        return None
    base = sum((dec(v) for v in window if v is not None), Decimal(0)) / avg_days
    if base <= 0:
        return None
    values = [dec(v) for v in inside if v is not None]
    return sum(values, Decimal(0)) / len(values) / base, min(values) / base


def dryup_on(params: BookVcpParams) -> bool:
    """Whether either dry-up rule is on."""
    return params.dryup_mean_max is not None or params.dryup_session_max is not None


def dryup_fault(mean: Decimal, lowest: Decimal, params: BookVcpParams) -> str | None:
    if params.dryup_mean_max is not None and mean >= dec(params.dryup_mean_max):
        return "no_volume_dryup"
    if params.dryup_session_max is not None and lowest >= dec(params.dryup_session_max):
        return "no_dryup_session"
    return None


def index_depth_pct(closes: Sequence[float]) -> Decimal | None:
    """The largest fall from a running peak to a later close, in percent; None without a close
    or with a non-positive one."""
    if not closes or min(closes) <= 0:
        return None
    peak, worst = dec(closes[0]), Decimal(0)
    for value in closes:
        close = dec(value)
        peak = max(peak, close)
        worst = max(worst, (peak - close) / peak * _HUNDRED)
    return worst


def relative_fault(first_depth: Decimal, idx_depth: Decimal, params: BookVcpParams) -> bool:
    """Whether the first contraction is too deep against the index's correction."""
    if params.rc_max_ratio is None:
        return False
    return first_depth > dec(params.rc_max_ratio) * idx_depth and first_depth > dec(
        params.rc_min_depth_pct
    )


TEMPLATE_CHECKS = (
    "close_above_sma150",
    "close_above_sma200",
    "sma150_above_sma200",
    "sma200_rising",
    "sma50_above_sma150",
    "sma50_above_sma200",
    "close_above_sma50",
    "above_52w_low",
    "near_52w_high",
)


def sma_series(c: np.ndarray, length: int) -> np.ndarray:
    """The simple moving average of `c` over `length` bars, NaN until the window is full."""
    out = np.full(len(c), np.nan)
    if len(c) >= length:
        sums = np.cumsum(np.insert(c, 0, 0.0))
        out[length - 1 :] = (sums[length:] - sums[:-length]) / length
    return out


def _rolling(values: np.ndarray, length: int, *, highest: bool) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) >= length:
        view = np.lib.stride_tricks.sliding_window_view(values, length)
        out[length - 1 :] = view.max(axis=1) if highest else view.min(axis=1)
    return out


def template_checks(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
    """(evaluable, each check of `TEMPLATE_CHECKS`, below_sma200) per bar: the trend
    template's eight price checks (spec B without the RS rank; the close above the SMA150 and
    the SMA200 is one check) in doubles, evaluable where every window is full, a check False
    where not; and whether the close is below its SMA200 (where that SMA exists)."""
    c = np.asarray(close, dtype=float)
    fast, mid, slow = sma_series(c, SMA_FAST), sma_series(c, SMA_MID), sma_series(c, SMA_SLOW)
    slow_before = np.full(len(c), np.nan)
    slow_before[SMA_RISING_SESSIONS:] = slow[:-SMA_RISING_SESSIONS]
    low52 = _rolling(np.asarray(low, dtype=float), WINDOW_52W, highest=False)
    high52 = _rolling(np.asarray(high, dtype=float), WINDOW_52W, highest=True)
    inputs = (c, fast, mid, slow, slow_before, low52, high52)
    evaluable = np.logical_and.reduce([np.isfinite(x) for x in inputs])
    with np.errstate(invalid="ignore"):
        checks = {
            "close_above_sma150": c > mid,
            "close_above_sma200": c > slow,
            "sma150_above_sma200": mid > slow,
            "sma200_rising": slow > slow_before,
            "sma50_above_sma150": fast > mid,
            "sma50_above_sma200": fast > slow,
            "close_above_sma50": c > fast,
            "above_52w_low": c >= LOW_52W_MIN * low52,
            "near_52w_high": c >= HIGH_52W_MIN * high52,
        }
        below = (c < slow) & np.isfinite(slow)
    return evaluable, {k: v & evaluable for k, v in checks.items()}, below


def template_passes(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(evaluable, passes, below_sma200) per bar (`template_checks`, every check passing)."""
    evaluable, checks, below = template_checks(high, low, close)
    return evaluable, np.logical_and.reduce(list(checks.values())), below


def count_start(
    evaluable: np.ndarray, passes: np.ndarray, below: np.ndarray, end: int, fail_sessions: int
) -> tuple[int | None, bool]:
    """(the first bar bases are counted from, stage2_start_unknown) for the signal bar `end`:
    the later of the last stage-2 start through `end` (a passing bar after at least
    `fail_sessions` consecutive evaluable failing ones) and the last close below the SMA200;
    without a stage-2 start, the first evaluable bar in its place. None without one."""
    stage2, failing, first = None, 0, None
    for i in range(end + 1):
        if not evaluable[i]:
            continue
        if first is None:
            first = i
        if passes[i]:
            if failing >= fail_sessions:
                stage2 = i
            failing = 0
        else:
            failing += 1
    unknown = stage2 is None
    start = first if unknown else stage2
    if start is None:
        return None, True
    reset = np.flatnonzero(below[: end + 1])
    if reset.size:
        start = max(start, int(reset[-1]))
    return start, unknown


def earlier_bases(
    bases: Mapping[int, tuple[Hashable, int] | None],
    close: Sequence[float],
    high: Sequence[float],
    start: int,
    end: int,
    current_h0: Hashable,
) -> int:
    """The earlier bases since bar `start`: distinct H0s other than `current_h0` of the bars
    `start..end - 1` whose geometry held (`bases`: bar -> (its H0, pivot bar) or None), whose
    pivot (that bar's high) a close after the bar, through `end`, went above."""
    later_max = [float("-inf")] * (end + 2)
    for i in range(end, start, -1):
        later_max[i] = max(later_max[i + 1], close[i])
    broken: set[Hashable] = set()
    for i in range(start, end):
        found = bases.get(i)
        if found is None or found[0] == current_h0 or found[0] in broken:
            continue
        if later_max[i + 1] > high[found[1]]:
            broken.add(found[0])
    return len(broken)


def footprint(sessions: int, contractions: Sequence[Contraction]) -> str:
    """'<weeks>W <Depth_1>/<Depth_final> <count>T', weeks the base's sessions / 5 and depths
    in whole percent, each rounded half up (e.g. '8W 22/2 3T')."""

    first, final = contractions[0].depth_pct, contractions[-1].depth_pct
    return footprint_text(sessions, first, final, len(contractions))


def footprint_text(sessions: int, first: Decimal, final: Decimal, count: int) -> str:
    """'<weeks>W <first>/<final> <count>T', weeks `sessions` / 5 and the depths in whole
    percent, each rounded half up."""

    def whole(value: Decimal) -> int:
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    return f"{whole(Decimal(sessions) / 5)}W {whole(first)}/{whole(final)} {count}T"


def shakeout(contractions: Sequence[Contraction]) -> bool:
    """Whether a contraction's low undercut an earlier contraction's low."""
    lows = [c.low for c in contractions]
    return any(low < min(lows[:i]) for i, low in enumerate(lows) if i)


# --- the criterion --------------------------------------------------------------------------


@dataclass(frozen=True)
class Series:
    dates: list[date]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float | None]


def series_of(ctx: EvalContext) -> Series | None:
    bars = ctx.history
    if not len(bars) or any(v is None for v in (*bars.high, *bars.low, *bars.close)):
        return None
    return Series(
        bars.dates,
        [float(v) for v in bars.high if v is not None],
        [float(v) for v in bars.low if v is not None],
        [float(v) for v in bars.close if v is not None],
        list(bars.volume),
    )


def _base_cache(
    ctx: EvalContext, series: Series, params: BookVcpParams
) -> MutableMapping[date, tuple[date, date] | None]:
    """The run's cache of earlier dates' geometry for this instrument (a fresh map outside a
    run): date -> (H0 date, pivot bar date) or None, each computed once from the history the
    scan held on the first night that needed it. Values are dates, so neither a later rescale of
    the history (a dividend) nor the scan's window moving on leaves them invalid; a date before
    a series break is never read again (the history starts at the break)."""
    if ctx.memo is None:
        return {}
    key = (_MEMO, ctx.instrument_id, params.geometry_key())
    found = ctx.memo.get(key)
    if found is None:
        found = {}
        ctx.memo[key] = found
    return found


def _bases_since(
    ctx: EvalContext,
    series: Series,
    threshold: Sequence[float],
    start: int,
    end: int,
    params: BookVcpParams,
) -> dict[int, tuple[date, int] | None]:
    """Bar -> (H0 date, pivot bar) for the bars `start..end - 1` whose geometry holds and whose
    pivot bar is still in the history (one before it cannot be priced on its scale)."""
    cache = _base_cache(ctx, series, params)
    index = {d: i for i, d in enumerate(series.dates)}
    out: dict[int, tuple[date, int] | None] = {}
    for i in range(start, end):
        day = series.dates[i]
        if day not in cache:
            geo = base_geometry(series.high, series.low, threshold, i, params)
            cache[day] = (
                (series.dates[geo.h0], series.dates[_pivot_bar(series.high, geo.final)])
                if isinstance(geo, Geometry)
                else None
            )
        found = cache[day]
        pivot = None if found is None else index.get(found[1])
        out[i] = None if found is None or pivot is None else (found[0], pivot)
    return out


def base_number(
    ctx: EvalContext, series: Series, threshold: Sequence[float], h0: int, params: BookVcpParams
) -> tuple[int, bool]:
    """(the base number of the base starting at bar `h0` on the signal bar, stage2_start_unknown):
    1 + the earlier bases of rule G counted since `count_start`."""
    end = len(series.dates) - 1
    evaluable, passes, below = template_passes(series.high, series.low, series.close)
    start, unknown = count_start(evaluable, passes, below, end, params.stage2_fail_sessions)
    earlier = 0
    if start is not None:
        bases = _bases_since(ctx, series, threshold, start, end, params)
        current = series.dates[h0]
        earlier = earlier_bases(bases, series.close, series.high, start, end, current)
    return 1 + earlier, unknown


def _pivot_bar(high: Sequence[float], final: Contraction) -> int:
    """The first bar of the final contraction whose high is its highest."""
    window = high[final.start : final.end + 1]
    return final.start + window.index(max(window))


def _benchmark_closes(ctx: EvalContext, first: date) -> list[float]:
    return [float(c) for d, c in ctx.benchmark if first <= d <= ctx.as_of and finite(float(c))]


def book_vcp(ctx: EvalContext, params: BookVcpParams) -> CriterionResult:
    """Passes with `pivot` and `final_contraction_low` (as `vcp` publishes them) and the
    signal's log: `footprint`, the contractions, `shakeout`, the time compression, the dry-up
    ratios, the relative correction, `base_number` and `stage2_start_unknown`; or fails with the
    first rule broken (`GATES`)."""
    series = series_of(ctx)
    if series is None:
        return no_data()
    end = len(series.dates) - 1
    threshold = swing_thresholds(series.high, series.low, series.close, params)
    geo = base_geometry(series.high, series.low, threshold, end, params)
    if not isinstance(geo, Geometry):
        return verdict(False, geo[1], geo[0])
    contractions = geo.contractions
    final = geo.final
    # D: time compression around the base's deepest low
    lows = series.low[geo.h0 : end + 1]
    deepest = geo.h0 + lows.index(min(lows))
    before, after = time_compression(geo.h0, deepest, end)
    fault = time_fault(before, after, params)
    if fault is not None:
        return verdict(False, float(after), fault)
    # E: dry-up over the final contraction
    # a rule switched off never rejects for want of its data
    ratios = dryup_ratios(series.volume, final, params.dryup_avg_days)
    if ratios is None and dryup_on(params):
        return verdict(False, None, "no_volume_data")
    fault = None if ratios is None else dryup_fault(*ratios, params)
    if ratios is not None and fault is not None:
        return verdict(False, float(ratios[0]), fault)
    # F: relative correction against the NIFTY 500
    first_depth = contractions[0].depth_pct
    idx_depth = index_depth_pct(_benchmark_closes(ctx, series.dates[geo.h0]))
    if idx_depth is None and params.rc_max_ratio is not None:
        return verdict(False, None, "no_benchmark")
    rc_ratio = None if not idx_depth else float(first_depth / idx_depth)
    if idx_depth is not None and relative_fault(first_depth, idx_depth, params):
        return verdict(False, rc_ratio, "correction_too_deep_vs_index")
    # G: base count
    number, unknown = base_number(ctx, series, threshold, geo.h0, params)
    if params.max_base_number is not None and number > params.max_base_number:
        return verdict(False, float(number), "late_stage_base")
    sessions = end - geo.h0 + 1
    extras: dict[str, Any] = {
        "pivot": float(final.high),
        "final_contraction_low": float(final.low),
        "contraction_count": len(contractions),
        "contractions": [
            {
                "start": series.dates[c.start].isoformat(),
                "end": series.dates[c.end].isoformat(),
                "depth_pct": float(c.depth_pct),
            }
            for c in contractions
        ],
        "base_start": series.dates[geo.h0].isoformat(),
        "base_length_sessions": sessions,
        "footprint": footprint(sessions, contractions),
        "shakeout": shakeout(contractions),
        "tc_low_sessions": before,
        "tc_after_sessions": after,
        "base_number": number,
        "stage2_start_unknown": unknown,
    }
    if ratios is not None:
        extras["dryup_mean_ratio"] = float(ratios[0])
        extras["dryup_min_ratio"] = float(ratios[1])
    if idx_depth is not None:
        extras["index_depth_pct"] = float(idx_depth)
    if rc_ratio is not None:
        extras["rc_ratio"] = rc_ratio
    return CriterionResult(True, 1.0, float(len(contractions)), REASON_OK, extras)
