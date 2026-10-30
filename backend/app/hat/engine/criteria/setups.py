"""The S1 setups beside `book_vcp` (Setup 1), each one hard setup criterion whose rules each have
their own parameter and failure reason, and the rules every setup shares (docs/decisions.md "S1
setups"). They read the bars dated on or before the signal date only and reuse `book`'s
zigzag swings (confirmed swings only), contractions, geometry rules, trend template and base
count.

Detectors (each publishes `pivot`, `final_contraction_low` (the tight area's low),
`base_length_sessions` and its log: `setup`, the tight area, the dry-up ratios, `footprint`,
`base_number`):

- `book_cheat` (Setup 2): a cup (`cup_of`: H0 the highest high of `h0_sessions`, C the lowest
  low after it, depth in `cup_depth_pct`, H0 to the signal date in `cup_sessions`) and a pause
  (`pause_of`: the longest run of the last `pause_sessions` bars whose range is at most
  `pause_range_max_pct`) at least `low_before_pause_sessions` after C. Its high's position in
  the cup names it `cheat_3c` (`cheat_band`) or `low_cheat` (below it); above it is not a cheat.
  The template's price checks passed on H0's bar (`h0_template`).
- `book_cup_handle` (Setup 3): the cup, and the handle from the highest high since C (the
  right lip) to the signal date: `handle_sessions` long, its low in the cup's upper part
  (`handle_low_min_fraction`), its high at most H0, at most `handle_depth_max_pct` deep. The
  handle is the tight area: a final tightening anchored at its high, as `book.tight_window`'s,
  starts at the lip, so it is the handle.
- `book_power_play` (Setup 4): T the highest high of the last `thrust_sessions`, at least
  `thrust_multiple` x the lowest low of the `thrust_base_sessions` before it; the flag from T to
  the signal date `flag_sessions` long and at most `flag_depth_max_pct` deep, with
  `min_contractions` contractions (zigzag from T and the tight pullback after them, as
  `book_vcp`'s, `book.chain_geometry`) each at most `depth_ratio_max` x the one before, the
  final at most `final_depth_max_pct`.
- `book_primary_base` (Setup 5): only on `history_sessions` of history (the scan calendar's
  sessions since the instrument's first bar, across gaps: `listed_sessions`); H0 the highest
  high, the base from it at least `base_min_sessions` long and at most `base_depth_max_pct`
  deep, with `contractions` like a VCP's (with the tight pullback); close above the SMA50 from
  `sma50_min_history` sessions and the eligible RS rank at least `rs_min` from
  `rs_min_history`.

A detector counts the base number by the book's default params. The tight area's dry-up
(`area_dryup`): each session's volume over ADV50, the mean of the 50 sessions before it
(`volume_ratio`); the mean below `dryup_mean_max` and one session below `dryup_session_max`.

Shared criteria, placed after a detector in its playbook and reading the levels it published:
`stop_cap` (the stop, the playbook's stop buffer below the tight area's low, at most `max_pct`
below the pivot), `overhead_supply` (rule B) and `setup_tags` (tags A, C, D, E; never fails).
A rule whose parameter is None is off. Depth rules compare exact decimals; the zigzag and the
template use doubles, as `book` does.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from typing import Any, ClassVar

import numpy as np
import polars as pl
from pydantic import Field, model_validator

from .base import (
    REASON_OK,
    RULE_STOP_BUFFER,
    CriterionResult,
    EvalContext,
    ParamModel,
    dec,
    no_data,
    verdict,
)
from .book import (
    BookVcpParams,
    Contraction,
    Geometry,
    Series,
    area,
    base_number,
    chain_geometry,
    contractions_of,
    footprint_text,
    series_of,
    sma_series,
    swing_thresholds,
    template_passes,
    zigzag,
)
from .setup import RsRankParams, SmaAboveParams, rs_rank, sma_above

_HUNDRED = Decimal(100)
ADV_SESSIONS = 50
CHEAT_3C, LOW_CHEAT = "cheat_3c", "low_cheat"
CUP_HANDLE, POWER_PLAY, PRIMARY_BASE = "cup_handle", "power_play", "primary_base"

# Each detector's failure reasons, in the order its rules run (the scan's funnel gates).
DRY_UP_REASONS = frozenset({"no_volume_data", "no_volume_dryup", "no_dryup_session"})
CUP_REASONS = frozenset({"no_cup", "cup_too_deep", "cup_too_short", "cup_too_long"})
GATES: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {
    "book_cheat": (
        ("cup", frozenset({"no_data", *CUP_REASONS})),
        (
            "pause",
            frozenset({"no_pause", "low_too_recent", "pause_in_upper_third", "h0_not_in_uptrend"}),
        ),
        ("dry_up", DRY_UP_REASONS),
    ),
    "book_cup_handle": (
        ("cup", frozenset({"no_data", *CUP_REASONS})),
        (
            "handle",
            frozenset(
                {
                    "handle_too_short",
                    "handle_too_long",
                    "handle_above_h0",
                    "handle_too_low",
                    "handle_too_deep",
                }
            ),
        ),
        ("dry_up", DRY_UP_REASONS),
    ),
    "book_power_play": (
        ("thrust", frozenset({"no_data", "no_thrust"})),
        (
            "flag",
            frozenset(
                {
                    "base_too_short",
                    "base_too_long",
                    "flag_too_deep",
                    "too_few_contractions",
                    "too_many_contractions",
                    "depth_ratio_too_wide",
                    "final_depth_too_deep",
                }
            ),
        ),
        ("dry_up", DRY_UP_REASONS),
    ),
    "book_primary_base": (
        (
            "primary_base",
            frozenset(
                {
                    "no_data",
                    "not_a_recent_listing",
                    "no_base",
                    "base_too_deep",
                    "base_too_short",
                    "too_few_contractions",
                    "too_many_contractions",
                    "depth_ratio_too_wide",
                    "final_depth_too_deep",
                }
            ),
        ),
        ("dry_up", DRY_UP_REASONS),
        ("primary_gates", frozenset({"below_sma50", "rs_below_min"})),
    ),
}


def gate_of(code: str, reason: str) -> str:
    """The funnel gate a failure `reason` of the detector `code` stops a stock-day at (its first
    gate when the reason is not listed)."""
    gates = GATES[code]
    for gate, reasons in gates:
        if reason in reasons:
            return gate
    return gates[0][0]


# --- shared pieces --------------------------------------------------------------------------


class _DryUp(ParamModel):
    # The values the plan marks [mine] (not from the source): fixed defaults, logged per signal.
    MINE: ClassVar[tuple[str, ...]] = ()

    dryup_mean_max: float | None = Field(default=0.7, gt=0, le=10)
    dryup_session_max: float | None = Field(default=0.5, gt=0, le=10)


class _Window(_DryUp):
    # The tight pullback after the last zigzag contraction (`book.tight_window`), as `book_vcp`'s.
    window_sessions: list[int] = Field(default_factory=lambda: [5, 15], min_length=2, max_length=2)

    @model_validator(mode="after")
    def _check_window(self) -> _Window:
        if not 1 <= self.window_sessions[0] <= self.window_sessions[1] <= 325:
            raise ValueError("window_sessions is [min, max] with 1 <= min <= max <= 325")
        return self


def mine(params: ParamModel) -> dict[str, Any]:
    """The [mine] values a rule ran with (`MINE`), for the signal's log."""
    return {name: getattr(params, name) for name in getattr(params, "MINE", ())}


def volume_ratio(volume: Sequence[float | None], i: int) -> Decimal | None:
    """vr of bar `i`: its volume over ADV50, the mean volume of the `ADV_SESSIONS` sessions
    before it; None without them, with a missing volume or a zero mean."""
    first = i - ADV_SESSIONS
    if first < 0 or volume[i] is None:
        return None
    window = volume[first:i]
    if any(v is None for v in window):
        return None
    base = sum((dec(v) for v in window if v is not None), Decimal(0)) / ADV_SESSIONS
    return None if base <= 0 else dec(volume[i]) / base


def area_dryup(volume: Sequence[float | None], area: Contraction) -> tuple[Decimal, Decimal] | None:
    """(mean, lowest) vr of the tight area's sessions; None when one has none."""
    ratios = [volume_ratio(volume, i) for i in range(area.start, area.end + 1)]
    if any(r is None for r in ratios):
        return None
    values = [r for r in ratios if r is not None]
    return sum(values, Decimal(0)) / len(values), min(values)


def dryup_fault(ratios: tuple[Decimal, Decimal] | None, params: _DryUp) -> str | None:
    """The first dry-up rule the tight area breaks (`no_volume_data` without its ratios while
    a rule is on), or None."""
    if params.dryup_mean_max is None and params.dryup_session_max is None:
        return None
    if ratios is None:
        return "no_volume_data"
    mean, lowest = ratios
    if params.dryup_mean_max is not None and mean >= dec(params.dryup_mean_max):
        return "no_volume_dryup"
    if params.dryup_session_max is not None and lowest >= dec(params.dryup_session_max):
        return "no_dryup_session"
    return None


@cache
def _book_params(**values: object) -> BookVcpParams:
    """The book's parameters with `values` (pairs as tuples, so they can be cached)."""
    return BookVcpParams.model_validate(
        {k: list(v) if isinstance(v, tuple) else v for k, v in values.items()}
    )


# The zigzag's reversal threshold: the book's (max(3%, 1.5 x ATR14 / close)).
_SWINGS = BookVcpParams()


def thresholds(series: Series) -> list[float]:
    return swing_thresholds(series.high, series.low, series.close, _SWINGS)


def _log(
    series: Series,
    setup: str,
    tight: Contraction,
    start: int,
    ratios: tuple[Decimal, Decimal] | None,
    deepest: Decimal,
    count: int,
    params: ParamModel,
) -> dict[str, Any]:
    """The extras every detector publishes: the levels, the base start, the tight area, the
    dry-up and the footprint (deepest / tightest)."""
    end = len(series.dates) - 1
    sessions = end - start + 1
    out: dict[str, Any] = {
        "setup": setup,
        "pivot": float(tight.high),
        "final_contraction_low": float(tight.low),
        "base_start": series.dates[start].isoformat(),
        "base_length_sessions": sessions,
        "tight_start": series.dates[tight.start].isoformat(),
        "tight_end": series.dates[tight.end].isoformat(),
        "tight_depth_pct": float(tight.depth_pct),
        "contraction_count": count,
        "footprint": footprint_text(sessions, deepest, tight.depth_pct, count),
        "mine": mine(params),
    }
    if ratios is not None:
        out["dryup_mean_vr"] = float(ratios[0])
        out["dryup_min_vr"] = float(ratios[1])
    return out


def _with_base_number(
    ctx: EvalContext, series: Series, threshold: Sequence[float], h0: int, log: dict[str, Any]
) -> CriterionResult:
    """Pass with `log` and the book's base number of the base from `h0` (a log, no rule), by
    the book's default params."""
    number, unknown = base_number(ctx, series, threshold, h0, _SWINGS)
    log["base_number"] = number
    log["stage2_start_unknown"] = unknown
    return CriterionResult(True, 1.0, log["tight_depth_pct"], REASON_OK, log)


# --- the cup (Setups 2 and 3) ---------------------------------------------------------------


class _CupParams(_DryUp):
    h0_sessions: int = Field(default=325, ge=20, le=700)
    cup_depth_pct: list[float] = Field(
        default_factory=lambda: [15.0, 50.0], min_length=2, max_length=2
    )
    cup_sessions: list[int] = Field(default_factory=lambda: [35, 325], min_length=2, max_length=2)

    @model_validator(mode="after")
    def _check_cup(self) -> _CupParams:
        if not 0 < self.cup_depth_pct[0] <= self.cup_depth_pct[1] <= 100:
            raise ValueError("cup_depth_pct is [min, max] within 0..100")
        if not 2 <= self.cup_sessions[0] <= self.cup_sessions[1]:
            raise ValueError("cup_sessions is [min, max] with 2 <= min <= max")
        return self


@dataclass(frozen=True)
class Cup:
    h0: int  # H0's bar: the first highest high of the window
    c: int  # C's bar: the first lowest low after H0
    top: Decimal
    bottom: Decimal

    @property
    def depth_pct(self) -> Decimal:
        return (self.top - self.bottom) / self.top * _HUNDRED

    def position(self, price: Decimal) -> Decimal:
        """Where `price` lies in the cup: 0 at C, 1 at H0."""
        return (price - self.bottom) / (self.top - self.bottom)


def cup_of(
    high: Sequence[float], low: Sequence[float], end: int, params: _CupParams
) -> Cup | tuple[str, float | None]:
    """The cup of the signal bar `end`, or (reason, value)."""
    first = max(0, end - params.h0_sessions + 1)
    window = high[first : end + 1]
    h0 = first + window.index(max(window))
    if h0 >= end:
        return "no_cup", None
    lows = low[h0 + 1 : end + 1]
    c = h0 + 1 + lows.index(min(lows))
    cup = Cup(h0, c, dec(high[h0]), dec(low[c]))
    depth = cup.depth_pct
    if depth < dec(params.cup_depth_pct[0]):
        return "no_cup", float(depth)
    if depth > dec(params.cup_depth_pct[1]):
        return "cup_too_deep", float(depth)
    length = end - h0 + 1
    if length < params.cup_sessions[0]:
        return "cup_too_short", float(length)
    if length > params.cup_sessions[1]:
        return "cup_too_long", float(length)
    return cup


def _cup_count(series: Series, threshold: Sequence[float], cup: Cup, end: int) -> int:
    """The contractions of the zigzag from H0 (the footprint's count)."""
    swings = zigzag(series.high, series.low, threshold, cup.h0, end)
    return len(contractions_of(series.high, series.low, swings))


# --- Setup 2: the cheat ---------------------------------------------------------------------


class BookCheatParams(_CupParams):
    MINE: ClassVar[tuple[str, ...]] = (
        "cup_sessions",
        "low_before_pause_sessions",
        "pause_sessions",
        "pause_range_max_pct",
        "h0_template",
    )

    pause_sessions: list[int] = Field(default_factory=lambda: [5, 15], min_length=2, max_length=2)
    pause_range_max_pct: float = Field(default=6.0, gt=0, le=100)
    low_before_pause_sessions: int = Field(default=10, ge=0, le=325)
    cheat_band: list[float] = Field(
        default_factory=lambda: [0.33, 0.67], min_length=2, max_length=2
    )
    h0_template: bool = True

    @model_validator(mode="after")
    def _check(self) -> BookCheatParams:
        if not 1 <= self.pause_sessions[0] <= self.pause_sessions[1]:
            raise ValueError("pause_sessions is [min, max] with 1 <= min <= max")
        if not 0 <= self.cheat_band[0] <= self.cheat_band[1] <= 1:
            raise ValueError("cheat_band is [low, high] within 0..1")
        return self


def pause_of(
    high: Sequence[float], low: Sequence[float], end: int, params: BookCheatParams
) -> Contraction | None:
    """The pause: the longest run of the last n bars through `end` (n in `pause_sessions`)
    whose range (highest high - lowest low) / highest high is at most `pause_range_max_pct`."""
    shortest, longest = params.pause_sessions
    limit = dec(params.pause_range_max_pct)
    for n in range(min(longest, end + 1), shortest - 1, -1):
        found = area(high, low, end - n + 1, end)
        if found.depth_pct <= limit:
            return found
    return None


def cheat_kind(position: Decimal, params: BookCheatParams) -> str | None:
    """`cheat_3c` inside `cheat_band`, `low_cheat` below it, None above it (a handle)."""
    low, high = (dec(v) for v in params.cheat_band)
    if position > high:
        return None
    return CHEAT_3C if position >= low else LOW_CHEAT


def book_cheat(ctx: EvalContext, params: BookCheatParams) -> CriterionResult:
    """Setup 2: passes with the cheat's levels and log (`cup_depth_pct`, `cup_position`), or
    fails with the first rule broken (`GATES`)."""
    series = series_of(ctx)
    if series is None:
        return no_data()
    end = len(series.dates) - 1
    cup = cup_of(series.high, series.low, end, params)
    if not isinstance(cup, Cup):
        return verdict(False, cup[1], cup[0])
    pause = pause_of(series.high, series.low, end, params)
    if pause is None:
        return verdict(False, None, "no_pause")
    if pause.start - cup.c < params.low_before_pause_sessions:
        return verdict(False, float(pause.start - cup.c), "low_too_recent")
    position = cup.position(pause.high)
    kind = cheat_kind(position, params)
    if kind is None:
        return verdict(False, float(position), "pause_in_upper_third")
    if params.h0_template:
        evaluable, passes, _ = template_passes(series.high, series.low, series.close)
        if not (evaluable[cup.h0] and passes[cup.h0]):
            return verdict(False, None, "h0_not_in_uptrend")
    ratios = area_dryup(series.volume, pause)
    fault = dryup_fault(ratios, params)
    if fault is not None:
        return verdict(False, None if ratios is None else float(ratios[0]), fault)
    threshold = thresholds(series)
    count = _cup_count(series, threshold, cup, end)
    log = _log(series, kind, pause, cup.h0, ratios, cup.depth_pct, count, params)
    log["cup_depth_pct"] = float(cup.depth_pct)
    log["cup_low_date"] = series.dates[cup.c].isoformat()
    log["cup_position"] = float(position)
    return _with_base_number(ctx, series, threshold, cup.h0, log)


# --- Setup 3: cup with handle ---------------------------------------------------------------


class BookCupHandleParams(_CupParams):
    MINE: ClassVar[tuple[str, ...]] = ("handle_sessions", "handle_depth_max_pct")

    handle_sessions: list[int] = Field(default_factory=lambda: [5, 25], min_length=2, max_length=2)
    handle_low_min_fraction: float = Field(default=0.67, ge=0, le=1)
    handle_depth_max_pct: float = Field(default=12.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> BookCupHandleParams:
        if not 1 <= self.handle_sessions[0] <= self.handle_sessions[1]:
            raise ValueError("handle_sessions is [min, max] with 1 <= min <= max")
        return self


def handle_of(
    high: Sequence[float], low: Sequence[float], cup: Cup, end: int, params: BookCupHandleParams
) -> Contraction | tuple[str, float]:
    """The handle: from the first highest high after C (the right lip) to `end`, or (reason,
    value) for the first rule it breaks."""
    right = high[cup.c + 1 : end + 1]
    if not right:
        return "handle_too_short", 0.0
    lip = cup.c + 1 + right.index(max(right))
    handle = area(high, low, lip, end)
    if handle.sessions < params.handle_sessions[0]:
        return "handle_too_short", float(handle.sessions)
    if handle.sessions > params.handle_sessions[1]:
        return "handle_too_long", float(handle.sessions)
    if handle.high > cup.top:
        return "handle_above_h0", float(handle.high)
    floor = cup.bottom + dec(params.handle_low_min_fraction) * (cup.top - cup.bottom)
    if handle.low < floor:
        return "handle_too_low", float(cup.position(handle.low))
    if handle.depth_pct > dec(params.handle_depth_max_pct):
        return "handle_too_deep", float(handle.depth_pct)
    return handle


def book_cup_handle(ctx: EvalContext, params: BookCupHandleParams) -> CriterionResult:
    """Setup 3: passes with the handle's levels and log, or fails with the first rule broken."""
    series = series_of(ctx)
    if series is None:
        return no_data()
    end = len(series.dates) - 1
    cup = cup_of(series.high, series.low, end, params)
    if not isinstance(cup, Cup):
        return verdict(False, cup[1], cup[0])
    handle = handle_of(series.high, series.low, cup, end, params)
    if not isinstance(handle, Contraction):
        return verdict(False, handle[1], handle[0])
    ratios = area_dryup(series.volume, handle)
    fault = dryup_fault(ratios, params)
    if fault is not None:
        return verdict(False, None if ratios is None else float(ratios[0]), fault)
    threshold = thresholds(series)
    count = _cup_count(series, threshold, cup, end)
    log = _log(series, CUP_HANDLE, handle, cup.h0, ratios, cup.depth_pct, count, params)
    log["cup_depth_pct"] = float(cup.depth_pct)
    log["cup_low_date"] = series.dates[cup.c].isoformat()
    return _with_base_number(ctx, series, threshold, cup.h0, log)


# --- contractions inside a base (Setups 4 and 5) --------------------------------------------


def base_contractions(
    series: Series, threshold: Sequence[float], start: int, end: int
) -> tuple[Contraction, ...]:
    """The contractions of the zigzag from bar `start` (a swing high) through `end`, without the
    tight pullback after them (`book.with_window`, which the detectors add)."""
    swings = zigzag(series.high, series.low, threshold, start, end)
    return tuple(contractions_of(series.high, series.low, swings))


# --- Setup 4: power play --------------------------------------------------------------------


class BookPowerPlayParams(_Window):
    MINE: ClassVar[tuple[str, ...]] = ("final_depth_max_pct",)

    thrust_sessions: int = Field(default=60, ge=2, le=325)
    thrust_base_sessions: int = Field(default=40, ge=1, le=325)
    thrust_multiple: float = Field(default=2.0, gt=1, le=100)
    flag_sessions: list[int] = Field(default_factory=lambda: [15, 30], min_length=2, max_length=2)
    flag_depth_max_pct: float = Field(default=25.0, gt=0, le=100)
    min_contractions: int = Field(default=2, ge=1, le=12)
    depth_ratio_max: float = Field(default=0.75, gt=0, le=1)
    final_depth_max_pct: float = Field(default=8.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> BookPowerPlayParams:
        if not 2 <= self.flag_sessions[0] <= self.flag_sessions[1]:
            raise ValueError("flag_sessions is [min, max] with 2 <= min <= max")
        return self

    def rules(self) -> BookVcpParams:
        """The flag's contraction rules as the book's geometry (no first-depth band, no final
        length rule; the base is the flag)."""
        return _book_params(
            contractions=(self.min_contractions, 12),
            first_depth_pct=(0.0, 100.0),
            depth_ratio_max=self.depth_ratio_max,
            final_depth_max_pct=self.final_depth_max_pct,
            final_min_sessions=1,
            window_sessions=tuple(self.window_sessions),
            base_sessions=tuple(self.flag_sessions),
        )


@dataclass(frozen=True)
class Thrust:
    top: int  # T's bar
    multiple: Decimal  # T's high over the lowest low of the bars before it


def thrust_of(
    high: Sequence[float], low: Sequence[float], end: int, params: BookPowerPlayParams
) -> Thrust | tuple[str, float | None]:
    """T the first highest high of the last `thrust_sessions` bars, at least `thrust_multiple`
    x the lowest low of the `thrust_base_sessions` bars before it; else (reason, value)."""
    first = max(0, end - params.thrust_sessions + 1)
    window = high[first : end + 1]
    top = first + window.index(max(window))
    if top < params.thrust_base_sessions:
        return "no_data", None
    base = dec(min(low[top - params.thrust_base_sessions : top]))
    multiple = dec(high[top]) / base if base > 0 else Decimal(0)
    if multiple < dec(params.thrust_multiple):
        return "no_thrust", float(multiple)
    return Thrust(top, multiple)


def book_power_play(ctx: EvalContext, params: BookPowerPlayParams) -> CriterionResult:
    """Setup 4: passes with the final contraction's levels and log (`thrust_multiple`,
    `flag_depth_pct`), or fails with the first rule broken."""
    series = series_of(ctx)
    if series is None:
        return no_data()
    end = len(series.dates) - 1
    thrust = thrust_of(series.high, series.low, end, params)
    if not isinstance(thrust, Thrust):
        return verdict(False, thrust[1], thrust[0])
    top = thrust.top
    rules = params.rules()
    length = end - top + 1
    if not rules.base_sessions[0] <= length <= rules.base_sessions[1]:
        reason = "base_too_short" if length < rules.base_sessions[0] else "base_too_long"
        return verdict(False, float(length), reason)
    flag = area(series.high, series.low, top, end)
    if flag.depth_pct > dec(params.flag_depth_max_pct):
        return verdict(False, float(flag.depth_pct), "flag_too_deep")
    threshold = thresholds(series)
    geo = chain_geometry(series.high, series.low, threshold, top, end, rules)
    if not isinstance(geo, Geometry):
        return verdict(False, geo[1], geo[0])
    contractions, final = geo.contractions, geo.final
    ratios = area_dryup(series.volume, final)
    fault_reason = dryup_fault(ratios, params)
    if fault_reason is not None:
        return verdict(False, None if ratios is None else float(ratios[0]), fault_reason)
    log = _log(series, POWER_PLAY, final, top, ratios, flag.depth_pct, len(contractions), params)
    log["thrust_multiple"] = float(thrust.multiple)
    log["flag_depth_pct"] = float(flag.depth_pct)
    return _with_base_number(ctx, series, threshold, top, log)


# --- Setup 5: primary base ------------------------------------------------------------------


class BookPrimaryBaseParams(_Window):
    MINE: ClassVar[tuple[str, ...]] = ("history_sessions",)

    history_sessions: list[int] = Field(
        default_factory=lambda: [40, 500], min_length=2, max_length=2
    )
    base_depth_max_pct: float = Field(default=35.0, gt=0, le=100)
    base_min_sessions: int = Field(default=15, ge=2, le=500)
    contractions: list[int] = Field(default_factory=lambda: [2, 6], min_length=2, max_length=2)
    depth_ratio_max: float = Field(default=0.75, gt=0, le=1)
    final_depth_max_pct: float = Field(default=10.0, gt=0, le=100)
    sma50_min_history: int | None = Field(default=50, ge=50, le=500)
    rs_min_history: int | None = Field(default=252, ge=1, le=500)
    rs_min: float = Field(default=70.0, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> BookPrimaryBaseParams:
        if not 2 <= self.history_sessions[0] <= self.history_sessions[1]:
            raise ValueError("history_sessions is [min, max] with 2 <= min <= max")
        if not 1 <= self.contractions[0] <= self.contractions[1] <= 12:
            raise ValueError("contractions is [min, max] with 1 <= min <= max <= 12")
        return self

    def rules(self, history: int) -> BookVcpParams:
        """The base's contraction rules as the book's geometry (no first-depth band, no final
        length rule)."""
        return _book_params(
            contractions=tuple(self.contractions),
            first_depth_pct=(0.0, 100.0),
            depth_ratio_max=self.depth_ratio_max,
            final_depth_max_pct=self.final_depth_max_pct,
            final_min_sessions=1,
            window_sessions=tuple(self.window_sessions),
            base_sessions=(self.base_min_sessions, max(self.base_min_sessions, history)),
        )


def primary_gate_fault(
    ctx: EvalContext, history: int, params: BookPrimaryBaseParams
) -> tuple[str, float | None] | None:
    """The gates that apply at this much history: close above the SMA50 from
    `sma50_min_history` sessions, the eligible RS rank at least `rs_min` from `rs_min_history`."""
    if params.sma50_min_history is not None and history >= params.sma50_min_history:
        found = sma_above(ctx, SmaAboveParams(a="close", b=50))
        if not found.passed:
            return "below_sma50", found.value
    if params.rs_min_history is not None and history >= params.rs_min_history:
        found = rs_rank(ctx, RsRankParams(min_percentile=params.rs_min, basis="eligible"))
        if not found.passed:
            return "rs_below_min", found.value
    return None


def listed_sessions(ctx: EvalContext) -> int | None:
    """The scan calendar's sessions (`EvalContext.calendar`) from the instrument's first bar
    through `as_of`, whatever it missed (a suspension, days without a trade); None when that
    first bar is on or before the calendar's first session (listed before the window, so no
    recent listing) or there is no calendar."""
    calendar = ctx.calendar
    dates = ctx.bars.filter(pl.col("date") <= ctx.as_of)["date"]
    if not calendar or dates.is_empty() or dates.min() <= calendar[0]:
        return None
    return len(calendar) - bisect_left(calendar, dates.min())


def book_primary_base(ctx: EvalContext, params: BookPrimaryBaseParams) -> CriterionResult:
    """Setup 5: passes with the final contraction's levels and log (`history_sessions`,
    `base_depth_pct`), or fails with the first rule broken."""
    series = series_of(ctx)
    if series is None:
        return no_data()
    end = len(series.dates) - 1
    history = listed_sessions(ctx)
    if history is None or not params.history_sessions[0] <= history <= params.history_sessions[1]:
        return verdict(False, None if history is None else float(history), "not_a_recent_listing")
    h0 = series.high.index(max(series.high))
    if h0 >= end:
        return verdict(False, None, "no_base")
    base = area(series.high, series.low, h0, end)
    if base.depth_pct > dec(params.base_depth_max_pct):
        return verdict(False, float(base.depth_pct), "base_too_deep")
    threshold = thresholds(series)
    geo = chain_geometry(series.high, series.low, threshold, h0, end, params.rules(history))
    if not isinstance(geo, Geometry):
        return verdict(False, geo[1], geo[0])
    contractions, final = geo.contractions, geo.final
    ratios = area_dryup(series.volume, final)
    fault_reason = dryup_fault(ratios, params)
    if fault_reason is not None:
        return verdict(False, None if ratios is None else float(ratios[0]), fault_reason)
    gate = primary_gate_fault(ctx, history, params)
    if gate is not None:
        return verdict(False, gate[1], gate[0])
    log = _log(series, PRIMARY_BASE, final, h0, ratios, base.depth_pct, len(contractions), params)
    log["history_sessions"] = history
    log["base_depth_pct"] = float(base.depth_pct)
    return _with_base_number(ctx, series, threshold, h0, log)


# --- shared criteria ------------------------------------------------------------------------


PIVOT, AREA_LOW, BASE_SESSIONS = "pivot", "final_contraction_low", "base_length_sessions"


def _level(ctx: EvalContext, name: str) -> Decimal | None:
    value = ctx.levels.get(name)
    return None if value is None else dec(value)


class StopCapParams(ParamModel):
    max_pct: float = Field(default=10.0, gt=0, le=100)


def stop_distance_pct(pivot: Decimal, low: Decimal, buffer_pct: float) -> Decimal:
    """How far below the pivot the stop `buffer_pct` under the tight area's low lies, in %."""
    stop = low * (1 - dec(buffer_pct) / _HUNDRED)
    return (pivot - stop) / pivot * _HUNDRED


def stop_cap(ctx: EvalContext, params: StopCapParams) -> CriterionResult:
    """The stop cap: fails `stop_too_wide` when the stop, the playbook's stop buffer under the
    final contraction's low (`EvalContext.rules`), lies more than `max_pct` below the pivot;
    publishes `stop_pct`. Outside a playbook it has no buffer and fails closed."""
    pivot, low = _level(ctx, PIVOT), _level(ctx, AREA_LOW)
    buffer_pct = ctx.rules.get(RULE_STOP_BUFFER)
    if pivot is None or low is None or pivot <= 0 or buffer_pct is None:
        return no_data()
    pct = stop_distance_pct(pivot, low, buffer_pct)
    if pct > dec(params.max_pct):
        return verdict(False, float(pct), "stop_too_wide")
    return CriterionResult(True, 1.0, float(pct), REASON_OK, {"stop_pct": float(pct)})


class OverheadSupplyParams(ParamModel):
    MINE: ClassVar[tuple[str, ...]] = ("above_pivot_max_pct", "fall_min_pct")

    sessions_ago: list[int] = Field(default_factory=lambda: [326, 750], min_length=2, max_length=2)
    above_pivot_max_pct: float = Field(default=10.0, ge=0, le=100)
    fall_min_pct: float = Field(default=30.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> OverheadSupplyParams:
        if not 1 <= self.sessions_ago[0] <= self.sessions_ago[1]:
            raise ValueError("sessions_ago is [min, max] with 1 <= min <= max")
        return self


@dataclass(frozen=True)
class Supply:
    index: int  # the old high's bar
    above_pct: Decimal  # its distance above the pivot


def overhead_supply_of(
    high: Sequence[float], low: Sequence[float], pivot: Decimal, params: OverheadSupplyParams
) -> Supply | None:
    """The latest high `sessions_ago` back (as far as the history goes) at most
    `above_pivot_max_pct` above the pivot and not below it, after which a low fell at least
    `fall_min_pct` below it through the signal bar; None without one."""
    end = len(high) - 1
    nearest, farthest = params.sessions_ago
    first, last = max(0, end - farthest), end - nearest
    ceiling = pivot * (1 + dec(params.above_pivot_max_pct) / _HUNDRED)
    keep = 1 - dec(params.fall_min_pct) / _HUNDRED
    lowest = float("inf")
    for j in range(end, first - 1, -1):
        if j <= last:
            old = dec(high[j])
            if pivot <= old <= ceiling and dec(lowest) <= old * keep:
                return Supply(j, (old - pivot) / pivot * _HUNDRED)
        lowest = min(lowest, low[j])
    return None


def overhead_supply(ctx: EvalContext, params: OverheadSupplyParams) -> CriterionResult:
    """Rule B: fails `overhead_supply` (value: the old high's distance above the pivot in %) with
    the old high's date, else passes."""
    series = series_of(ctx)
    pivot = _level(ctx, PIVOT)
    if series is None or pivot is None or pivot <= 0:
        return no_data()
    found = overhead_supply_of(series.high, series.low, pivot, params)
    if found is None:
        return CriterionResult(True, 1.0, None, REASON_OK, {"mine": mine(params)})
    extras = {
        "supply_high_date": series.dates[found.index].isoformat(),
        "supply_above_pct": float(found.above_pct),
        "mine": mine(params),
    }
    return CriterionResult(False, 0.0, float(found.above_pct), "overhead_supply", extras)


class SetupTagsParams(ParamModel):
    MINE: ClassVar[tuple[str, ...]] = (
        "shakeout_undercut_max_pct",
        "shakeout_vr_min",
        "leader_index_below_high_pct",
        "stage1_band_pct",
        "stage3_sessions",
    )

    shakeout_undercut_max_pct: float = Field(default=5.0, ge=0, le=100)
    shakeout_recover_sessions: int = Field(default=3, ge=1, le=20)
    shakeout_vr_min: float = Field(default=1.2, ge=0, le=100)
    leader_sessions: int = Field(default=252, ge=20, le=700)
    leader_index_below_high_pct: float = Field(default=10.0, ge=0, le=100)
    rs_min: float = Field(default=70.0, ge=0, le=100)
    stage1_slope_pct: float = Field(default=1.0, ge=0, le=100)
    stage1_band_pct: float = Field(default=15.0, ge=0, le=100)
    stage3_sessions: int = Field(default=60, ge=1, le=500)


def shakeout_of(
    series: Series, threshold: Sequence[float], start: int, params: SetupTagsParams
) -> Decimal | None:
    """Tag A: the undercut % of the latest confirmed swing low of the base from `start` that
    undercut an earlier one of the base by at most `shakeout_undercut_max_pct`, after which a
    close within `shakeout_recover_sessions` sessions went back above the earlier low on vr at
    least `shakeout_vr_min` (the first such close); None without one."""
    end = len(series.dates) - 1
    swings = zigzag(series.high, series.low, threshold, start, end)
    lows = [s for s in swings if not s.high]
    limit = dec(params.shakeout_undercut_max_pct)
    for k in range(len(lows) - 1, 0, -1):
        later = lows[k]
        for earlier in reversed(lows[:k]):
            undercut = (dec(earlier.price) - dec(later.price)) / dec(earlier.price) * _HUNDRED
            if not 0 < undercut <= limit:
                continue
            last = min(end, later.index + params.shakeout_recover_sessions)
            back = next(
                (r for r in range(later.index + 1, last + 1) if series.close[r] > earlier.price),
                None,
            )
            vr = None if back is None else volume_ratio(series.volume, back)
            if vr is not None and vr >= dec(params.shakeout_vr_min):
                return undercut
    return None


def early_leader(
    dates: Sequence[date],
    close: Sequence[float],
    benchmark: Sequence[tuple[date, float]],
    params: SetupTagsParams,
) -> bool | None:
    """Tag D: the RS line (close / NIFTY 500 close, on the sessions both have) at its high of
    the last `leader_sessions` of them while the index closes at least
    `leader_index_below_high_pct` below its own high of those sessions; None without that many
    or without the signal date's index close."""
    index = dict(benchmark)
    if not dates or dates[-1] not in index:
        return None
    pairs = [(c, index[d]) for d, c in zip(dates, close, strict=True) if d in index]
    window = pairs[-params.leader_sessions :]
    if len(window) < params.leader_sessions or any(i <= 0 for _, i in window):
        return None
    line = [c / i for c, i in window]
    closes = [i for _, i in window]
    index_low = closes[-1] <= (1 - params.leader_index_below_high_pct / 100) * max(closes)
    return line[-1] >= max(line) and index_low


STAGE_1, STAGE_2, STAGE_3, STAGE_4 = "stage_1", "stage_2", "stage_3", "stage_4"
UNCLASSIFIED = "unclassified"


def stage_of(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    rs: float | None,
    params: SetupTagsParams,
) -> str:
    """Tag E on the last bar: stage 2 when the trend template passes (the eight price checks
    and the RS rank); stage 4 when the close is below a falling SMA200 (against 21 sessions
    before); stage 1 when that SMA200 is within `stage1_slope_pct` of its value then and the
    close within `stage1_band_pct` of it; stage 3 when the price checks passed on one of the
    `stage3_sessions` sessions before (no earlier RS rank is in the history); else
    unclassified."""
    end = len(close) - 1
    evaluable, passes, _ = template_passes(high, low, close)
    if evaluable[end] and passes[end] and rs is not None and rs >= params.rs_min:
        return STAGE_2
    slow = sma_series(np.asarray(close, dtype=float), 200)
    if end >= 21 and np.isfinite(slow[end]) and np.isfinite(slow[end - 21]):
        now, before, last = dec(float(slow[end])), dec(float(slow[end - 21])), dec(close[end])
        if last < now and now < before:
            return STAGE_4
        slope = abs(now / before - 1) * _HUNDRED
        band = abs(last / now - 1) * _HUNDRED
        if slope <= dec(params.stage1_slope_pct) and band <= dec(params.stage1_band_pct):
            return STAGE_1
    if passes[max(0, end - params.stage3_sessions) : end].any():
        return STAGE_3
    return UNCLASSIFIED


def ath_pivot(high: Sequence[float], start: int, pivot: Decimal) -> bool | None:
    """Tag C: the pivot at or above the highest high before the base (bar `start`) in the
    history read; None without a bar before it."""
    return None if start <= 0 else pivot >= dec(max(high[:start]))


def setup_tags(ctx: EvalContext, params: SetupTagsParams) -> CriterionResult:
    """Tags A, C, D and E of the setup the earlier criteria published (no effect on a trade;
    it never fails): `shakeout` with `shakeout_undercut_pct`, `ath_pivot`, `early_leader` and
    `stage`; a tag without its input is None."""
    series = series_of(ctx)
    if series is None:
        return CriterionResult(True, 1.0, None, REASON_OK)
    end = len(series.dates) - 1
    rs = rs_rank(ctx, RsRankParams(basis="eligible")).value
    tags: dict[str, Any] = {
        "stage": stage_of(series.high, series.low, series.close, rs, params),
        "early_leader": early_leader(series.dates, series.close, ctx.benchmark, params),
        "mine": mine(params),
    }
    pivot, sessions = _level(ctx, PIVOT), ctx.levels.get(BASE_SESSIONS)
    if sessions is not None and 1 <= int(sessions) <= end + 1:
        start = end - int(sessions) + 1
        undercut = shakeout_of(series, thresholds(series), start, params)
        tags["shakeout"] = undercut is not None
        if undercut is not None:
            tags["shakeout_undercut_pct"] = float(undercut)
        if pivot is not None:
            tags["ath_pivot"] = ath_pivot(series.high, start, pivot)
    return CriterionResult(True, 1.0, None, REASON_OK, tags)


def swing_high_above(series: Series, start: int, level: Decimal) -> bool:
    """Whether bars `start..` confirm a swing high above `level`: the highest high since
    `start` is above it and a later bar's low reaches that high x (1 - its zigzag threshold),
    as `book.zigzag` confirms a high (the trigger window's early cancel)."""
    threshold = thresholds(series)
    peak: float | None = None
    for i in range(max(0, start), len(series.dates)):
        if peak is None or series.high[i] > peak:
            peak = series.high[i]
        elif dec(peak) > level and series.low[i] <= peak * (1 - threshold[i]):
            return True
    return False
