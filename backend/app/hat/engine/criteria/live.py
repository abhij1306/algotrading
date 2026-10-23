"""The live trigger and entry-filter criteria (HAT-CRIT-115..119, 121, 122).

They read a `LiveContext`: what the trigger monitor knows at the instant a 5-minute bar is final
(the bar's high, the live price, the cumulative day volume, the opening range, the pre-open gap,
the instrument's announcements) plus the daily values the nightly data holds (average volumes,
ADTR, the previous close). Like the daily criteria they are pure: nothing here reads a clock, the
database or the network, and a missing input fails closed with reason `no_data`.

Every comparison is exact: prices are `Decimal`, volumes integers, and the elapsed share of the
session a `Fraction`, so a value exactly on a threshold is a tie, never a rounding accident.
`neglect`, a trigger-stage criterion over daily bars, is evaluated in `setup.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import Field

from .base import CriterionResult, ParamModel, dec, no_data, verdict

LEVEL_PIVOT = "pivot"
LEVEL_OPENING_RANGE = "opening_range_high"
LEVEL_BREAKOUT = "max(consolidation_high, opening_range_high)"
LEVELS = (LEVEL_PIVOT, LEVEL_OPENING_RANGE, LEVEL_BREAKOUT)
CONSOLIDATION_HIGH = "consolidation_high"

REASON_BELOW_LEVEL = "below_level"
REASON_LEVEL_UNDEFINED = "level_undefined"
REASON_GAP_TOO_SMALL = "gap_too_small"
REASON_LOW_PACE = "volume_pace_low"
REASON_LOW_EARLY_VOLUME = "early_volume_low"
REASON_NO_CATALYST = "no_catalyst"
REASON_STOP_TOO_WIDE = "stop_wider_than_adtr"
REASON_STOP_NOT_BELOW = "stop_not_below_entry"
REASON_CHASED = "price_past_max_chase"
REASON_UPPER_CIRCUIT = "locked_upper_circuit"
_HUNDRED = Decimal(100)


class GapUpParams(ParamModel):
    min_pct: float = Field(default=10.0, gt=0, le=1000)


class VolumePaceParams(ParamModel):
    vs_avg50: float = Field(default=1.5, gt=0, le=1000)


class EarlyVolumeParams(ParamModel):
    vs_adv20: float = Field(default=0.5, gt=0, le=1000)


class CatalystAttachedParams(ParamModel):
    within_hours: int = Field(default=24, ge=1, le=168)
    required_in: list[Literal["shadow", "confirm", "auto"]] = Field(
        default_factory=lambda: ["confirm"], max_length=3
    )


class PriceBreaksLevelParams(ParamModel):
    level: Literal["pivot", "opening_range_high", "max(consolidation_high, opening_range_high)"]
    bar: Literal["5m"] = "5m"
    # `close`: the bar must also close at or above the level, so a spike that fell back below it
    # is no break (PR 49); `high`: a trade above the level is enough
    confirm: Literal["close", "high"] = "close"


class AdtrFilterParams(ParamModel):
    max_multiple: float = Field(default=1.0, gt=0, le=100)


class MaxChaseParams(ParamModel):
    pct: float = Field(gt=0, le=100)


@dataclass(frozen=True)
class LiveContext:
    """What a live criterion may read for one instrument at one instant.

    `levels` names the reference prices (`pivot`, `consolidation_high`, `opening_range_high`); the
    opening range is absent until its bar is final. `elapsed` is the share of the session gone at
    `at`, in (0, 1]. `announcements` are the `published_at` instants of the announcements matched
    to the instrument. `locked_upper_circuit` is None when it cannot be told (no band known)."""

    at: datetime
    price: Decimal | None = None
    bar_high: Decimal | None = None
    bar_close: Decimal | None = None
    levels: Mapping[str, Decimal | None] = field(default_factory=dict)
    cum_volume: int | None = None
    elapsed: Fraction | None = None
    avg50_volume: Decimal | None = None
    adv20: Decimal | None = None
    open_price: Decimal | None = None
    prev_close: Decimal | None = None
    announcements: Sequence[datetime] = ()
    entry: Decimal | None = None
    stop: Decimal | None = None
    adtr20: Decimal | None = None
    trigger_level: Decimal | None = None
    locked_upper_circuit: bool | None = None

    def for_entry(
        self,
        *,
        entry: Decimal,
        stop: Decimal | None,
        adtr20: Decimal | None,
        trigger_level: Decimal,
        locked_upper_circuit: bool | None,
    ) -> LiveContext:
        """This context with the entry-filter inputs of a trigger's plan set."""
        return LiveContext(
            at=self.at,
            price=self.price,
            bar_high=self.bar_high,
            bar_close=self.bar_close,
            levels=self.levels,
            cum_volume=self.cum_volume,
            elapsed=self.elapsed,
            avg50_volume=self.avg50_volume,
            adv20=self.adv20,
            open_price=self.open_price,
            prev_close=self.prev_close,
            announcements=self.announcements,
            entry=entry,
            stop=stop,
            adtr20=adtr20,
            trigger_level=trigger_level,
            locked_upper_circuit=locked_upper_circuit,
        )


def _pos(value: object) -> Decimal | None:
    """`value` when it is a positive finite Decimal, else None."""
    return value if isinstance(value, Decimal) and value.is_finite() and value > 0 else None


def resolve_level(expression: str, levels: Mapping[str, Decimal | None]) -> Decimal | None:
    """The price a `price_breaks_level` expression names (HAT-BOT-071): the pivot, the opening
    range high, or the larger of the consolidation high and the opening range high. None while
    any level it needs is unknown, so the breakout level does not exist before 09:20."""
    if expression == LEVEL_PIVOT:
        names: tuple[str, ...] = (LEVEL_PIVOT,)
    elif expression == LEVEL_OPENING_RANGE:
        names = (LEVEL_OPENING_RANGE,)
    elif expression == LEVEL_BREAKOUT:
        names = (CONSOLIDATION_HIGH, LEVEL_OPENING_RANGE)
    else:
        return None
    values = [_pos(levels.get(name)) for name in names]
    known = [v for v in values if v is not None]
    return max(known) if len(known) == len(names) else None


def price_breaks_level(ctx: LiveContext, p: PriceBreaksLevelParams) -> CriterionResult:
    """HAT-CRIT-119: the final bar traded strictly above the level (its high is above it) and,
    with `confirm: close` (the default, PR 49), closed at or above it. The level is reported as
    the value and in `extras["level"]`, the trigger price of the signal."""
    level = resolve_level(p.level, ctx.levels)
    if level is None:
        return no_data(REASON_LEVEL_UNDEFINED)
    high = _pos(ctx.bar_high)
    close = _pos(ctx.bar_close)
    if high is None or (p.confirm == "close" and close is None):
        return no_data()
    held = p.confirm == "high" or (close is not None and close >= level)
    result = verdict(high > level and held, float(level), REASON_BELOW_LEVEL)
    return CriterionResult(
        result.passed, result.score, result.value, result.reason, {"level": level}
    )


def gap_up(ctx: LiveContext, p: GapUpParams) -> CriterionResult:
    """HAT-CRIT-115: (open / previous close - 1) x 100 >= min_pct."""
    opened, prev = _pos(ctx.open_price), _pos(ctx.prev_close)
    if opened is None or prev is None:
        return no_data()
    gap = 100 * (Fraction(opened) / Fraction(prev) - 1)
    return verdict(gap >= Fraction(dec(p.min_pct)), float(gap), REASON_GAP_TOO_SMALL)


def projected_volume(cum_volume: int, elapsed: Fraction) -> Fraction:
    """The day's volume projected to the full session: V / f."""
    return Fraction(cum_volume) / elapsed


def volume_pace(ctx: LiveContext, p: VolumePaceParams) -> CriterionResult:
    """HAT-CRIT-116: V / f >= vs_avg50 x the 50-day average volume. With no elapsed share of
    the session (before the first bar is final) it does not pass."""
    avg50 = _pos(ctx.avg50_volume)
    if ctx.cum_volume is None or ctx.cum_volume < 0 or avg50 is None:
        return no_data()
    if ctx.elapsed is None or not 0 < ctx.elapsed <= 1:
        return no_data()
    projected = projected_volume(ctx.cum_volume, ctx.elapsed)
    needed = Fraction(dec(p.vs_avg50)) * Fraction(avg50)
    return verdict(projected >= needed, float(projected), REASON_LOW_PACE)


def early_volume(ctx: LiveContext, p: EarlyVolumeParams) -> CriterionResult:
    """HAT-CRIT-117: cumulative day volume at the trigger >= vs_adv20 x ADV(20)."""
    adv20 = _pos(ctx.adv20)
    if ctx.cum_volume is None or ctx.cum_volume < 0 or adv20 is None:
        return no_data()
    needed = Fraction(dec(p.vs_adv20)) * Fraction(adv20)
    return verdict(ctx.cum_volume >= needed, float(ctx.cum_volume), REASON_LOW_EARLY_VOLUME)


def catalyst_attached(ctx: LiveContext, p: CatalystAttachedParams) -> CriterionResult:
    """HAT-CRIT-118: an announcement matched to the instrument was published in
    [at - within_hours, at]. Whether a failure blocks the trigger is the trigger stage's rule
    (`required_in`), not this function's."""
    since = ctx.at - timedelta(hours=p.within_hours)
    found = [t for t in ctx.announcements if since <= t <= ctx.at]
    newest = max(found, default=None)
    hours = None if newest is None else (ctx.at - newest).total_seconds() / 3600
    return verdict(newest is not None, hours, REASON_NO_CATALYST)


def adtr_filter(ctx: LiveContext, p: AdtrFilterParams) -> CriterionResult:
    """HAT-CRIT-121: entry - stop <= max_multiple x ADTR(20)."""
    entry, stop, adtr = _pos(ctx.entry), _pos(ctx.stop), _pos(ctx.adtr20)
    if entry is None or stop is None or adtr is None:
        return no_data()
    if stop >= entry:
        return no_data(REASON_STOP_NOT_BELOW)
    width = entry - stop
    passed = width <= dec(p.max_multiple) * adtr
    return verdict(passed, float(width / adtr), REASON_STOP_TOO_WIDE)


def max_chase(ctx: LiveContext, p: MaxChaseParams) -> CriterionResult:
    """HAT-CRIT-122: price <= level x (1 + pct/100) and the stock is not locked at its upper
    circuit. Unknown circuit state fails closed."""
    price, level = _pos(ctx.price), _pos(ctx.trigger_level)
    if price is None or level is None or ctx.locked_upper_circuit is None:
        return no_data()
    if ctx.locked_upper_circuit:
        return CriterionResult(False, 0.0, float(price), REASON_UPPER_CIRCUIT)
    passed = price * _HUNDRED <= level * (_HUNDRED + dec(p.pct))
    chased = (price / level - 1) * _HUNDRED
    return verdict(passed, float(chased), REASON_CHASED)
