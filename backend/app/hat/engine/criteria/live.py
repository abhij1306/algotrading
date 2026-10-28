"""The live trigger and entry-filter criteria (HAT-CRIT-115..119, 121, 122, 127).

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

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ...governance import Mode, ModeField
from .base import CriterionResult, ParamModel, dec, no_data, verdict

# The one level the live monitor supplies itself, once the 09:15 bar is final; every other level
# is a setup criterion's (`CriterionType.levels`), stored with its watchlist row.
LEVEL_OPENING_RANGE = "opening_range_high"
# A level expression: one level's name, or `max(a, b, ...)` of several.
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_MAX = re.compile(r"max\((.+)\)")

REASON_BELOW_LEVEL = "below_level"
REASON_LEVEL_UNDEFINED = "level_undefined"
REASON_DAILY_CLOSE_NOT_LIVE = "daily_close_not_live"
INTRADAY_BREAK = "intraday_break"
DAILY_CLOSE = "daily_close"
REASON_GAP_TOO_SMALL = "gap_too_small"
REASON_LOW_PACE = "volume_pace_low"
REASON_LOW_EARLY_VOLUME = "early_volume_low"
REASON_NO_CATALYST = "no_catalyst"
REASON_NO_RESULTS = "no_results_filing"
REASON_GROWTH_UNDEFINED = "growth_undefined"
REASON_GROWTH_LOW = "growth_below_min"
REASON_STOP_TOO_WIDE = "stop_wider_than_adtr"
REASON_STOP_NOT_BELOW = "stop_not_below_entry"
REASON_CHASED = "price_past_max_chase"
REASON_UPPER_CIRCUIT = "locked_upper_circuit"
_HUNDRED = Decimal(100)


class GapUpParams(ParamModel):
    min_pct: float = Field(default=10.0, gt=0, le=1000)


class VolumePaceParams(ParamModel):
    vs_avg50: float = Field(default=1.5, gt=0, le=1000)


EARLY_AT_TRIGGER = "at_trigger"
EARLY_TIME_ANCHORED = "time_anchored"
# `time_anchored`: the full ADV(20) is due this many minutes after the open, pro rata before.
ANCHOR_MINUTES = 30


class EarlyVolumeParams(ParamModel):
    """`at_trigger` (the default, left out of the canonical form): the day volume at the trigger
    is at least `vs_adv20` x ADV(20). `time_anchored`: at least ADV(20) x min(1, minutes /
    `ANCHOR_MINUTES`), minutes from the open to the trigger bar's close (`vs_adv20` unused)."""

    vs_adv20: float = Field(default=0.5, gt=0, le=1000)
    mode: Literal["at_trigger", "time_anchored"] = Field(
        default=EARLY_AT_TRIGGER, exclude_if=lambda v: v == EARLY_AT_TRIGGER
    )


class EarningsCatalystParams(ParamModel):
    """HAT-CRIT-127 (plan B1): sales_yoy >= `sales_yoy_pct` and (eps_yoy >= `eps_yoy_pct`, or a
    turnaround, or sales_yoy >= `sales_alone_pct`), percentages."""

    sales_yoy_pct: float = Field(default=25.0, ge=-100, le=100_000)
    eps_yoy_pct: float = Field(default=25.0, ge=-100, le=100_000)
    sales_alone_pct: float = Field(default=50.0, ge=-100, le=100_000)


class CatalystAttachedParams(ParamModel):
    within_hours: int = Field(default=24, ge=1, le=168)
    required_in: list[ModeField] = Field(default_factory=lambda: [Mode.CONFIRM], max_length=3)


def level_names(expression: str) -> tuple[str, ...]:
    """The levels a `price_breaks_level` expression names, in order: `name` or `max(a, b, ...)`
    of distinct names. Raises `ValueError("bad_level_expression")` for anything else."""
    text = expression.strip()
    found = _MAX.fullmatch(text)
    names = tuple(n.strip() for n in found.group(1).split(",")) if found else (text,)
    if not all(_NAME.fullmatch(n) for n in names) or len(set(names)) != len(names):
        raise ValueError("bad_level_expression")
    if found and len(names) < 2:
        raise ValueError("bad_level_expression")
    return names


class PriceBreaksLevelParams(ParamModel):
    """`level`: the level expression (`level_names`), each name a level the playbook's setup
    criteria expose or the opening range high (`config.Playbook` checks which)."""

    level: str
    bar: Literal["5m"] = "5m"
    # `close`: the bar must also close at or above the level, so a spike that fell back below it
    # is no break (PR 49); `high`: a trade above the level is enough
    confirm: Literal["close", "high"] = "close"
    # When the entry is taken. `intraday_break`: on the break of the level during the session, as
    # live's 5-minute trigger takes it. `daily_close`: at the session's close when the close is
    # above the level, a backtest variant; live does not trade it (`daily_close_not_live`).
    entry_timing: Literal["intraday_break", "daily_close"] = Field(
        default=INTRADAY_BREAK, exclude_if=lambda v: v == INTRADAY_BREAK
    )
    # Under `daily_close` only: the breakout session's volume at least `volume_vs_avg` x the mean
    # volume of the `volume_avg_days` sessions before it (None: no volume test). An intraday
    # break cannot know the day's volume, so it refuses the test.
    volume_vs_avg: float | None = Field(default=None, gt=0, le=100, exclude_if=lambda v: v is None)
    volume_avg_days: int = Field(default=50, ge=5, le=252, exclude_if=lambda v: v == 50)

    @field_validator("level")
    @classmethod
    def _expression(cls, value: str) -> str:
        level_names(value)
        return value

    @model_validator(mode="after")
    def _volume_at_the_close(self) -> PriceBreaksLevelParams:
        if self.volume_vs_avg is not None and self.entry_timing != DAILY_CLOSE:
            raise ValueError("breakout_volume_needs_daily_close")
        return self


class AdtrFilterParams(ParamModel):
    max_multiple: float = Field(default=1.0, gt=0, le=100)


class MaxChaseParams(ParamModel):
    pct: float = Field(gt=0, le=100)


@dataclass(frozen=True)
class ResultEvent:
    """One stored results filing as `earnings_catalyst` reads it: when it became public and the
    growth of its quarter as known then (`hat_financial_growth`, ratios: 0.25 is 25%)."""

    public_at: datetime
    period_end: date
    sales_yoy: Decimal | None = None
    eps_yoy: Decimal | None = None
    turnaround: bool | None = None


@dataclass(frozen=True)
class LiveContext:
    """What a live criterion may read for one instrument at one instant.

    `levels` names the reference prices: the setup's levels stored with the watchlist row (such as
    `pivot` or `consolidation_high`) and `opening_range_high`, absent until its bar is final.
    `elapsed` is the share of the session gone at `at`, in (0, 1]. `announcements` are the
    `published_at` instants of the announcements matched to the instrument.
    `locked_upper_circuit` is None when it cannot be told (no band known). `minutes` are the
    minutes from the session open to the close of the bar being judged. `results` are the
    instrument's results filings (`ResultEvent`) that may fall in `earnings_catalyst`'s window,
    which opens at `results_since` (the previous session's open); the criterion reads only those
    public by `at`."""

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
    minutes: Fraction | None = None
    results: Sequence[ResultEvent] = ()
    results_since: datetime | None = None

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
        return copy.replace(
            self,
            entry=entry,
            stop=stop,
            adtr20=adtr20,
            trigger_level=trigger_level,
            locked_upper_circuit=locked_upper_circuit,
        )


def _pos(value: object) -> Decimal | None:
    """`value` when it is a positive finite Decimal, else None."""
    return value if isinstance(value, Decimal) and value.is_finite() and value > 0 else None


def resolve_level(
    expression: str, levels: Mapping[str, Decimal | None], *, daily: bool = False
) -> Decimal | None:
    """The price a `price_breaks_level` expression names (HAT-BOT-071): the one level, or the
    largest of `max(...)`'s. None while any level it needs is unknown, so a breakout level with the
    opening range does not exist before 09:20. The one resolver of live, the scan and the
    backtest: `daily` reads the opening range as unavailable on daily bars (the backtest's known
    gap `trigger_is_level_break_only`): it drops out of a `max`, and a level of it alone is
    None."""
    names = level_names(expression)
    if daily:
        names = tuple(n for n in names if n != LEVEL_OPENING_RANGE)
    values = [_pos(levels.get(name)) for name in names]
    known = [v for v in values if v is not None]
    return max(known) if known and len(known) == len(names) else None


def price_breaks_level(ctx: LiveContext, p: PriceBreaksLevelParams) -> CriterionResult:
    """HAT-CRIT-119: the final bar traded strictly above the level (its high is above it) and,
    with `confirm: close` (the default, PR 49), closed at or above it. The level is reported as
    the value and in `extras["level"]`, the trigger price of the signal. An `entry_timing:
    daily_close` level never breaks live (`daily_close_not_live`): that timing is a backtest
    variant, and its known gap blocks the version's promotion."""
    if p.entry_timing == DAILY_CLOSE:
        return no_data(REASON_DAILY_CLOSE_NOT_LIVE)
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


def early_volume_needed(adv20: Decimal, p: EarlyVolumeParams, minutes: Fraction) -> Fraction:
    """The day volume `early_volume` requires `minutes` after the open (`EarlyVolumeParams`)."""
    if p.mode == EARLY_TIME_ANCHORED:
        return Fraction(adv20) * min(Fraction(1), minutes / ANCHOR_MINUTES)
    return Fraction(dec(p.vs_adv20)) * Fraction(adv20)


def early_volume(ctx: LiveContext, p: EarlyVolumeParams) -> CriterionResult:
    """HAT-CRIT-117: cumulative day volume at the trigger >= the volume `early_volume_needed`
    names; `time_anchored` without the minutes since the open is `no_data`."""
    adv20 = _pos(ctx.adv20)
    if ctx.cum_volume is None or ctx.cum_volume < 0 or adv20 is None:
        return no_data()
    if p.mode == EARLY_TIME_ANCHORED and (ctx.minutes is None or ctx.minutes <= 0):
        return no_data()
    needed = early_volume_needed(adv20, p, ctx.minutes or Fraction(0))
    return verdict(ctx.cum_volume >= needed, float(ctx.cum_volume), REASON_LOW_EARLY_VOLUME)


def results_filing(
    results: Sequence[ResultEvent], since: datetime, at: datetime
) -> ResultEvent | None:
    """The newest filing public in (since, at], None without one; the latest-dated wins a tie of
    instants (a revision filed with its original)."""
    found = [r for r in results if since < r.public_at <= at]
    return max(found, key=lambda r: (r.public_at, r.period_end), default=None)


def growth_passes(event: ResultEvent, p: EarningsCatalystParams) -> bool | None:
    """B1's growth rule on one filing; None when its sales growth is undefined."""
    if event.sales_yoy is None:
        return None
    sales = event.sales_yoy * _HUNDRED
    if sales < dec(p.sales_yoy_pct):
        return False
    eps_ok = event.eps_yoy is not None and event.eps_yoy * _HUNDRED >= dec(p.eps_yoy_pct)
    return eps_ok or bool(event.turnaround) or sales >= dec(p.sales_alone_pct)


def _ratio_pct(value: Decimal | None) -> float | None:
    return None if value is None else float(value * _HUNDRED)


def earnings_catalyst(ctx: LiveContext, p: EarningsCatalystParams) -> CriterionResult:
    """HAT-CRIT-127 (plan B1): a results filing of the instrument public in (the previous
    session's open, the trigger instant] (`results_filing`) whose growth passes
    (`growth_passes`). The value is its sales growth in percent; `extras` log the filing's
    `results_public_at`, `sales_yoy_pct`, `eps_yoy_pct` and `turnaround` on every evaluation
    that found one. Without the window's start it is `no_data`."""
    if ctx.results_since is None:
        return no_data()
    event = results_filing(ctx.results, ctx.results_since, ctx.at)
    if event is None:
        return CriterionResult(False, 0.0, None, REASON_NO_RESULTS)
    extras = {
        "results_public_at": event.public_at.isoformat(),
        "sales_yoy_pct": _ratio_pct(event.sales_yoy),
        "eps_yoy_pct": _ratio_pct(event.eps_yoy),
        "turnaround": event.turnaround,
    }
    passed = growth_passes(event, p)
    reason = REASON_GROWTH_UNDEFINED if passed is None else REASON_GROWTH_LOW
    result = verdict(bool(passed), _ratio_pct(event.sales_yoy), reason)
    return CriterionResult(result.passed, result.score, result.value, result.reason, extras)


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
    return chase_within(ctx, dec(p.pct))


def chase_within(ctx: LiveContext, pct: Decimal) -> CriterionResult:
    """`max_chase` on an exact percentage (the Take re-checks the one fixed at the trigger)."""
    price, level = _pos(ctx.price), _pos(ctx.trigger_level)
    if price is None or level is None or ctx.locked_upper_circuit is None:
        return no_data()
    if ctx.locked_upper_circuit:
        return CriterionResult(False, 0.0, float(price), REASON_UPPER_CIRCUIT)
    passed = price * _HUNDRED <= level * (_HUNDRED + pct)
    chased = (price / level - 1) * _HUNDRED
    return verdict(passed, float(chased), REASON_CHASED)
