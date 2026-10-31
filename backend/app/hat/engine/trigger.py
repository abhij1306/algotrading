"""The trigger monitor: live trigger and entry-filter evaluation on final 5-minute bars
(HAT-EXEC-120..122, HAT-CRIT-115..122, HAT-BOT-052, HAT-BOT-071).

Pure and synchronous, like the bar builder it listens to: `TriggerMonitor.on_bar` takes one final
bar and returns the triggers it fired, and reads only the candidates it was given and the bar
view (opening range, day volume). Nothing here reads a clock, the database or the network;
`live_entry.py` loads the candidates and turns a trigger into a signal.

Rules:

- A trigger is evaluated only on a final bar (the builder hands over a bar at its end + 2 s),
  and only when that instant lies in the trigger window: from 5 minutes after the session open to
  10 minutes before its continuous trading ends ([09:20, 15:05] IST in a regular session, the
  closing auction ending continuous trading at 15:15), both from the day's own hours (`in_window`);
  the trigger's time is the bar's final instant (HAT-EXEC-120).
- A playbook triggers when every enabled trigger criterion passes on the same bar (HAT-EXEC-121).
  A trigger type over daily bars (`neglect`) is evaluated once, on the bars before the trade
  date, when the candidate is built (`daily_trigger_results`); without that result it fails
  closed.
  `catalyst_attached` is the one exception: its result is recorded, but it blocks only when the
  bot's mode is in its `required_in` list (HAT-CRIT-118, HAT-EXEC-034). A playbook with no
  enabled trigger criterion never triggers.
- `price_breaks_level` breaks when the bar's high is strictly above the level and, with
  `confirm: close` (the default), its close is at or above it; the trigger price is the level.
  The level of an opening-range expression does not exist before the first bar is final, so
  nothing fires before 09:20 (HAT-CRIT-119, HAT-BOT-071). A level with `entry_timing:
  daily_close` (a backtest variant) never breaks live (`daily_close_not_live`).
- The entry is a LIMIT at the larger of the level and the bar's close, rounded up to the tick, so
  it never buys below the level it broke (`entry_limit`, PR 49); `max_chase`
  checks that price.
- A playbook whose level trigger is `entry_timing: stop_order` or `next_session`
  (`Playbook.on_ticks`) fires on a trade, not on a final bar (`on_tick`, decisions.md "Setup
  entry orders"): a stop order on the first trade above the level in the trigger window, a
  next-session entry on the first trade in it after a previous close above the level. Its other
  trigger criteria read the day volume of the final bars so far. The entry is that trade's price
  and its order a LIMIT a small band over it, at most the max-chase price (`EntryPlan.limit`,
  `stop_limit`): a buy stop-limit, which `max_chase` checks at the trade's price. `on_bar` never
  fires one.
- At the trigger the initial stop is computed with the instrument's real tick (HAT-EXEC-053),
  then the entry filters run in their configured order; `max_chase` always runs, with the
  playbook's last configured percentage, even when the config deleted or disabled it
  (HAT-CRIT-122). The first failure names the skip reason (HAT-EXEC-122). The day low comes from
  the bar view, which has none for a session not watched from the open or with a feed gap, so a
  `low_of_day` stop then skips with `stop_input_missing` (PR 22).
- One (bot, playbook, instrument, trade date) has at most one signal. A fired key is pending
  until its signal write reports back: `confirm` closes it for the day, `release` (a failed
  write) lets the next final bar evaluate it again, so a lost write never loses the day.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from fractions import Fraction
from typing import Protocol

import polars as pl

from ..data.bars import BAR_WIDTH, Bar, Tick
from ..data.series_break import segment_start
from ..data.sessions import SESSION_CLOSE, SESSION_OPEN
from ..money import exact, require_exact
from ..risk.caps import effective_max_chase_pct
from ..risk.sessions import CLOSING_AUCTION
from ..time import IST
from .criteria import (
    CONTRACTION_LOW,
    CriterionResult,
    EvalContext,
    evaluate,
    evaluate_live,
    get_type,
)
from .criteria.live import (
    LEVEL_OPENING_RANGE,
    REASON_GAP_TOO_SMALL,
    REASON_LOW_PACE,
    LiveContext,
    ResultEvent,
    VolumePaceParams,
    level_names,
    resolve_level,
    results_filing,
)
from .stops import (
    SKIP_BAD_INPUT,
    SeriesHistory,
    StopInputs,
    StopPlan,
    floor_to_tick,
    history_inputs,
    plan_stop,
)
from .strategy.config import Instance, Playbook, StopRule, StrategyConfig

__all__ = [
    "CONTRACTION_LOW",
    "TRIGGER_WINDOW",
    "BarView",
    "Candidate",
    "CriterionOutcome",
    "CriterionResult",
    "REASON_GAP_TOO_SMALL",
    "ResultEvent",
    "results_filing",
    "DailyInputs",
    "EntryPlan",
    "Trigger",
    "TriggerEvaluation",
    "TriggerMonitor",
    "DailyFeeds",
    "daily_context",
    "daily_inputs",
    "daily_trigger_results",
    "elapsed_fraction",
    "entry_filter_context",
    "chase_limit",
    "entry_limit",
    "first_above",
    "stop_limit",
    "entry_stop",
    "entry_filter_instances",
    "evaluate_entry_filters",
    "evaluate_trigger",
    "last_configured_chase",
    "locked_lower_circuit",
    "locked_upper_circuit",
    "lower_circuit_price",
    "minutes_since_open",
    "plan_entry",
    "upper_circuit_price",
    "daily_level",
    "volume_pace_multiple",
    "watchlist_levels",
]

# The trigger window's ends against the session's open and its continuous-trading end.
TRIGGER_WINDOW = (timedelta(minutes=5), timedelta(minutes=10))
MAX_CHASE = "max_chase"
CATALYST = "catalyst_attached"
PRICE_BREAKS_LEVEL = "price_breaks_level"
SKIP_NO_LEVEL = "level_undefined"
SKIP_NO_PRICE = "no_live_price"
AVG_VOLUME_DAYS = 50
VOLUME_PACE = "volume_pace"
PACE_LOW = REASON_LOW_PACE  # `volume_pace` failed: too little volume for the time of day
_HUNDRED = Decimal(100)
# How far over the triggering trade a stop order's LIMIT may reach (percent of that price): its
# fill, sized at that price, costs at most this much more per share, and a live price past the
# LIMIT refuses the placement (`router`, decisions.md "Setup entry orders").
ENTRY_BAND_PCT = Decimal("0.5")


class BarView(Protocol):
    """What the monitor reads from the bar builder besides the bar itself."""

    def opening_range(self, symbol: str, day: date) -> tuple[Decimal, Decimal] | None: ...

    def session_start(self, day: date) -> datetime: ...

    def session_end(self, day: date) -> datetime: ...

    def day_volume(self, symbol: str, day: date) -> int: ...

    def day_low(
        self, symbol: str, day: date, through: datetime | None = None
    ) -> Decimal | None: ...


@dataclass(frozen=True)
class DailyInputs:
    """The nightly values one instrument's live evaluation reads (from adjusted daily bars and
    features up to the previous session). `band_known` False means no price-band row: the
    circuit state cannot be told. `band_pct` None with a known band is an F&O name (no band)."""

    prev_close: Decimal | None = None
    avg50_volume: Decimal | None = None
    adv20: Decimal | None = None
    adtr20: Decimal | None = None
    tick: Decimal | None = None
    band_known: bool = False
    band_pct: Decimal | None = None
    atr: Decimal | None = None
    swing_lows: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class DailyFeeds:
    """The `DailyInputs` values that do not come from the instrument's daily bars: the tick
    (`data.ticks` on the unadjusted closes), the `adv20`/`adtr20` features and the price band."""

    tick: Decimal | None
    adv20: Decimal | None = None
    adtr20: Decimal | None = None
    band_known: bool = False
    band_pct: Decimal | None = None


def daily_inputs(
    bars: pl.DataFrame, trade_date: date, feeds: DailyFeeds, stop: StopRule | None = None
) -> DailyInputs:
    """`DailyInputs` from one instrument's adjusted daily bars (`date`, `high`, `low`, `close`,
    `volume`) and its `feeds`: the previous close is the last close before `trade_date`
    (HAT-CRIT-115), the average volume the mean of the last 50 sessions' volume before it
    (HAT-DATA-115; None with fewer than 50), and the ATR and swing lows those of the `stop`
    rule at its own length (`stops.history_inputs`; none without a rule), all from the bars since
    the last series break (`series_break`)."""
    prior = bars.filter(pl.col("date") < trade_date).sort("date")
    prior = prior.slice(segment_start(prior["date"].to_list(), trade_date))  # since a series break
    closes = [exact(v, floats=True) for v in prior["close"].to_list()] if prior.height else []
    volumes = [exact(v, floats=True) for v in prior["volume"].to_list()] if prior.height else []
    recent = volumes[-AVG_VOLUME_DAYS:]
    avg50 = (
        sum((v for v in recent if v is not None), Decimal(0)) / AVG_VOLUME_DAYS
        if len(recent) == AVG_VOLUME_DAYS and all(v is not None for v in recent)
        else None
    )
    atr, swing = (
        (None, ())
        if stop is None or not prior.height
        else history_inputs(
            stop, SeriesHistory.of_floats(*(prior[c].to_list() for c in ("high", "low", "close")))
        )
    )
    return DailyInputs(
        prev_close=closes[-1] if closes else None,
        avg50_volume=avg50,
        adv20=feeds.adv20,
        adtr20=feeds.adtr20,
        tick=feeds.tick,
        band_known=feeds.band_known,
        band_pct=feeds.band_pct,
        atr=atr,
        swing_lows=tuple(swing),
    )


@dataclass(frozen=True)
class Candidate:
    """One (bot, playbook, instrument) the monitor watches on `trade_date`.

    `levels` holds the setup levels stored with the watchlist row (`watchlist_levels`), which the
    playbook's level expression reads with the opening range; `open_price` is the day's
    open from the pre-open capture (EP); `announcements` are the `published_at` instants matched
    to the instrument. `configured_max_chase_pct` is the playbook's last configured max-chase
    percentage across the bot's versions (None when it never had one). `daily_results` holds the
    results of the playbook's daily-bar trigger criteria by instance order. `results` are the
    instrument's results filings public after `results_since` (the previous session's open), as
    `earnings_catalyst` reads them (`LiveContext.results`). `universe` is the playbook's
    effective universe stage and `series` the instrument's, which a pre-open playbook's setup
    check judges (`live_entry.ep_setup_passes`)."""

    bot_id: uuid.UUID
    strategy_key: str
    mode: str
    playbook: Playbook
    instrument_id: uuid.UUID
    symbol: str
    trade_date: date
    daily: DailyInputs
    levels: Mapping[str, Decimal | None] = field(default_factory=dict)
    final_contraction_low: Decimal | None = None
    open_price: Decimal | None = None
    announcements: tuple[datetime, ...] = ()
    configured_max_chase_pct: Decimal | None = None
    daily_results: Mapping[int, CriterionResult] = field(default_factory=dict)
    results: tuple[ResultEvent, ...] = ()
    results_since: datetime | None = None
    universe: tuple[Instance, ...] = ()
    series: str | None = None

    @property
    def key(self) -> tuple[uuid.UUID, str, uuid.UUID, date]:
        return (self.bot_id, self.playbook.name, self.instrument_id, self.trade_date)


@dataclass(frozen=True)
class CriterionOutcome:
    type: str
    order: int
    blocking: bool
    result: CriterionResult

    def to_json(self) -> dict[str, object]:
        return {
            "type": self.type,
            "order": self.order,
            "blocking": self.blocking,
            "passed": self.result.passed,
            "value": self.result.value,
            "reason": self.result.reason,
        }


@dataclass(frozen=True)
class TriggerEvaluation:
    fired: bool
    outcomes: tuple[CriterionOutcome, ...]
    level: Decimal | None


@dataclass(frozen=True)
class EntryPlan:
    """The entry at a trigger: price, level and stop, or the skip reason (a stop rule's reason,
    or the type of the first failing entry filter)."""

    entry: Decimal | None
    level: Decimal | None
    stop: Decimal | None
    locked_upper_circuit: bool | None
    skip: str | None
    filters: tuple[CriterionOutcome, ...]
    # the order's LIMIT when it is not `entry`: a stop order's (`stop_limit`)
    limit: Decimal | None = None


@dataclass(frozen=True)
class Trigger:
    candidate: Candidate
    at: datetime  # the final instant of the bar that fired
    evaluation: TriggerEvaluation
    plan: EntryPlan
    context: LiveContext


# --- pure pieces ------------------------------------------------------------------------------


def elapsed_fraction(
    at: datetime, *, session_open: time = SESSION_OPEN, session_close: time = SESSION_CLOSE
) -> Fraction | None:
    """The share of the session gone at `at`, in (0, 1]; None outside (open, close]."""
    local = at.astimezone(IST)
    opens = datetime.combine(local.date(), session_open, tzinfo=IST)
    closes = datetime.combine(local.date(), session_close, tzinfo=IST)
    if not opens < local <= closes:
        return None
    micro = timedelta(microseconds=1)
    return Fraction((local - opens) // micro, (closes - opens) // micro)


def minutes_since_open(bar: Bar, session_start: datetime) -> Fraction:
    """The minutes from the session open to the close of `bar` (its start plus its width)."""
    return Fraction(int((bar.start + BAR_WIDTH - session_start).total_seconds()), 60)


def upper_circuit_price(daily: DailyInputs) -> Decimal | None:
    """The upper band price: previous close x (1 + band/100), on the tick grid when the tick is
    known. None without a band (an F&O name) or a previous close."""
    prev, band = daily.prev_close, daily.band_pct
    if prev is None or band is None or prev <= 0:
        return None
    raw = prev * (_HUNDRED + band) / _HUNDRED
    return floor_to_tick(raw, daily.tick) if daily.tick is not None else raw


def locked_upper_circuit(price: Decimal, daily: DailyInputs) -> bool | None:
    """Whether `price` is at the upper circuit: False for an F&O name (no band), None when the
    band or the previous close is unknown (the caller fails closed)."""
    if not daily.band_known:
        return None
    if daily.band_pct is None:
        return False
    upper = upper_circuit_price(daily)
    return None if upper is None else price >= upper


def lower_circuit_price(daily: DailyInputs) -> Decimal | None:
    """The lower band price: previous close x (1 - band/100), up to the tick grid when the tick
    is known (PR 51). None without a band (an F&O name) or a previous close."""
    prev, band = daily.prev_close, daily.band_pct
    if prev is None or band is None or prev <= 0:
        return None
    raw = prev * (_HUNDRED - band) / _HUNDRED
    if daily.tick is None or daily.tick <= 0:
        return raw
    return (raw / daily.tick).to_integral_value(rounding=ROUND_CEILING) * daily.tick


def locked_lower_circuit(price: Decimal, daily: DailyInputs) -> bool | None:
    """Whether `price` is at the lower circuit, read from the stored band as the upper one is:
    False for an F&O name, None when the band or the previous close is unknown (PR 51)."""
    if not daily.band_known:
        return None
    if daily.band_pct is None:
        return False
    lower = lower_circuit_price(daily)
    return None if lower is None else price <= lower


def last_configured_chase(configs: Sequence[StrategyConfig], playbook: str) -> Decimal | None:
    """The playbook's last configured max-chase percentage, newest config first: the first
    config whose playbook holds a `max_chase` instance (enabled or not) gives it."""
    for config in configs:
        for book in config.playbooks:
            if book.name != playbook:
                continue
            for instance in book.entry_filters:
                if instance.type == MAX_CHASE:
                    return require_exact(instance.params["pct"], "pct", floats=True)
    return None


def entry_filter_instances(
    playbook: Playbook, configured_pct: Decimal | None, strategy_key: str
) -> list[Instance]:
    """The entry filters evaluated at a trigger: the enabled ones in order, with `max_chase`
    always present (HAT-CRIT-122). An enabled `max_chase` keeps its place and percentage; a
    disabled or deleted one is evaluated last with the last configured percentage, else the
    strategy's documented default."""
    enabled = [i for i in playbook.entry_filters if i.enabled]
    if any(i.type == MAX_CHASE for i in enabled):
        return enabled
    pct = effective_max_chase_pct(configured_pct, strategy_key)
    last_order = max((i.order for i in playbook.entry_filters), default=-1)
    # Built without validation: it is evaluated after every configured filter, and its order may
    # pass the config's limit (a filter at the highest allowed order). Its fields are fixed and
    # known valid; the parameters are validated again when it is evaluated.
    forced = Instance.model_construct(
        type=MAX_CHASE,
        stage="entry_filter",
        mode="hard",
        weight=1.0,
        params={"pct": float(pct)},
        enabled=True,
        order=last_order + 1,
    )
    return [*enabled, forced]


def is_daily_trigger(code: str) -> bool:
    """A trigger type evaluated over daily bars (no live evaluator), such as `neglect`."""
    return get_type(code).evaluate_live is None


def daily_trigger_results(
    playbook: Playbook, bars: pl.DataFrame, trade_date: date
) -> dict[int, CriterionResult]:
    """The playbook's enabled daily-bar trigger criteria evaluated on the bars before
    `trade_date` (the trade date's own bar does not exist yet), keyed by instance order."""
    ctx = EvalContext(
        as_of=trade_date, instrument_id="", bars=bars.filter(pl.col("date") < trade_date)
    )
    return {
        i.order: evaluate(i.type, i.params, ctx)
        for i in playbook.trigger
        if i.enabled and is_daily_trigger(i.type)
    }


def evaluate_trigger(
    playbook: Playbook,
    ctx: LiveContext,
    mode: str,
    daily_results: Mapping[int, CriterionResult] | None = None,
) -> TriggerEvaluation:
    """Every enabled trigger criterion, in order, on one bar; fired iff each blocking one passed
    (and there is at least one). A daily-bar criterion takes its result from `daily_results`
    and fails closed without one. The level is the `price_breaks_level` level, when there is
    one."""
    daily = daily_results or {}
    outcomes: list[CriterionOutcome] = []
    level: Decimal | None = None
    for instance in playbook.trigger:
        if not instance.enabled:
            continue
        result = daily.get(instance.order) if is_daily_trigger(instance.type) else None
        if result is None:
            result = evaluate_live(instance.type, instance.params, ctx)
        blocking = instance.type != CATALYST or mode in instance.params.get("required_in", ())
        outcomes.append(CriterionOutcome(instance.type, instance.order, blocking, result))
        if instance.type == PRICE_BREAKS_LEVEL and result.passed and level is None:
            level = result.extras.get("level")  # the one enabled instance (`one_trigger_level`)
    blocking_results = [o.result.passed for o in outcomes if o.blocking]
    fired = bool(blocking_results) and all(blocking_results)
    return TriggerEvaluation(fired, tuple(outcomes), level)


def first_above(level: Decimal, tick: Decimal) -> Decimal:
    """The first price on the `tick` grid strictly above `level`: level + tick for a level on
    the grid, the next grid price for one between (an adjusted level). A buy stop's trigger."""
    return (level / tick).to_integral_value(rounding=ROUND_FLOOR) * tick + tick


def chase_limit(level: Decimal, pct: Decimal, tick: Decimal | None) -> Decimal:
    """The highest price `max_chase` at `pct` allows over `level` (level x (1 + pct/100)), down
    to the tick when it is known: a stop order's LIMIT, so it never fills past the chase."""
    raw = level * (_HUNDRED + pct) / _HUNDRED
    return raw if tick is None or tick <= 0 else floor_to_tick(raw, tick)


def stop_limit(entry: Decimal, chase: Decimal, tick: Decimal | None) -> Decimal:
    """A stop order's LIMIT: the triggering trade's price `entry` plus `ENTRY_BAND_PCT` (down to
    the tick), at most the max-chase price `chase`, never below `entry`. The position is sized
    at `entry`, so a fill at the LIMIT risks at most the band more per share."""
    band = entry * (_HUNDRED + ENTRY_BAND_PCT) / _HUNDRED
    if tick is not None and tick > 0:
        band = floor_to_tick(band, tick)
    return max(entry, min(band, chase))


def chase_pct(playbook: Playbook, configured_pct: Decimal | None, strategy_key: str) -> Decimal:
    """The max-chase percentage a trigger is checked at (`entry_filter_instances`): the least
    of the enabled `max_chase` filters', else the forced one's."""
    return min(
        require_exact(i.params["pct"], "pct", floats=True)
        for i in entry_filter_instances(playbook, configured_pct, strategy_key)
        if i.type == MAX_CHASE
    )


def entry_limit(level: Decimal, price: Decimal, tick: Decimal | None) -> Decimal:
    """The entry LIMIT price at a trigger (PR 49): the larger of the level and the
    live price (the trigger bar's close), rounded up to the tick when it is known, so the order
    never buys below the level it broke."""
    limit = max(level, price)
    if tick is None or tick <= 0:
        return limit
    return ceil_to_tick(limit, tick)


def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def plan_entry(
    candidate: Candidate,
    ctx: LiveContext,
    level: Decimal | None,
    day_lows: Sequence[Decimal] = (),
) -> EntryPlan:
    """The stop and the entry filters at the trigger. The entry price is `entry_limit` of the
    level and `ctx.price` (the trigger bar's close), and `day_lows` the lows of the day's final
    bars up to and including the trigger bar."""
    price = ctx.price
    if price is None or price <= 0:
        return EntryPlan(None, level, None, None, SKIP_NO_PRICE, ())
    if level is None:
        locked = locked_upper_circuit(price, candidate.daily)
        return EntryPlan(price, None, None, locked, SKIP_NO_LEVEL, ())
    tick = candidate.daily.tick
    limit = None
    if candidate.playbook.on_ticks:
        # a trade's price: the order is a LIMIT a band over it within the chase, a stop-limit
        entry = price if tick is None or tick <= 0 else ceil_to_tick(price, tick)
        pct = chase_pct(
            candidate.playbook, candidate.configured_max_chase_pct, candidate.strategy_key
        )
        limit = stop_limit(entry, chase_limit(level, pct, tick), tick)
        upper = upper_circuit_price(candidate.daily)
        if upper is not None and entry <= upper < limit:
            limit = upper  # the exchange refuses a buy above the band
    else:
        entry = entry_limit(level, price, tick)
    locked = locked_upper_circuit(entry, candidate.daily)
    planned = entry_stop(
        candidate.playbook, candidate.daily, entry, day_lows, candidate.final_contraction_low
    )
    # the filters read the LIMIT (max_chase reads `price`), with the trigger's other inputs
    failed, outcomes = evaluate_entry_filters(
        candidate.playbook,
        entry_filter_context(ctx, entry=entry, stop=planned.stop, level=level, locked=locked),
        configured_pct=candidate.configured_max_chase_pct,
        strategy_key=candidate.strategy_key,
    )
    return EntryPlan(entry, level, planned.stop, locked, planned.skip or failed, outcomes, limit)


def watchlist_levels(stored: Mapping[str, object]) -> dict[str, Decimal | None]:
    """The setup levels of a watchlist row (`criteria_results.levels`, every level its setup
    criteria exposed), a level that is not a positive price None."""
    return {name: exact(value, floats=True, positive=True) for name, value in stored.items()}


def daily_level(playbook: Playbook, levels: Mapping[str, Decimal | None]) -> Decimal | None:
    """The playbook's trigger level on daily data: its `price_breaks_level` expression over the
    setup `levels`, the opening range unavailable (`resolve_level(daily=True)`). The scan stores
    it as the row's `pivot` (the overnight stop plan, the watchlist); the backtest breaks, chases
    and ranks on it. None without such a trigger or without its levels."""
    expression = playbook.level_expression
    return None if expression is None else resolve_level(expression, levels, daily=True)


def volume_pace_multiple(playbook: Playbook) -> float | None:
    """The `vs_avg50` the playbook's enabled `volume_pace` triggers need, the strictest when it
    holds several (each blocks live), None without one (the backtest's daily estimate judges it
    on the full day's volume)."""
    multiples = [
        VolumePaceParams.model_validate(i.params).vs_avg50
        for i in playbook.trigger
        if i.enabled and i.type == VOLUME_PACE
    ]
    return max(multiples, default=None)


def needs_setup_level(playbook: Playbook) -> bool:
    """The playbook's trigger level names a setup level, not only the opening range. A setup can
    pass on score while the score criterion that exposes that level failed, so the scan keeps
    such a candidate off the watchlist (`daily_level` None): every candidate the live trigger and
    the backtest read then has its trigger level."""
    expression = playbook.level_expression
    return expression is not None and any(
        name != LEVEL_OPENING_RANGE for name in level_names(expression)
    )


def daily_context(
    at: datetime, daily: DailyInputs, levels: Mapping[str, Decimal | None]
) -> LiveContext:
    """What a live criterion reads at `at` from the nightly data: the average volumes, ADTR(20),
    the previous close and the setup's `levels`. The live trigger adds the bar's own values
    (`TriggerMonitor.context`); the backtest, on daily bars, has none of them (HAT-BT-025)."""
    return LiveContext(
        at=at,
        levels=levels,
        avg50_volume=daily.avg50_volume,
        adv20=daily.adv20,
        prev_close=daily.prev_close,
        adtr20=daily.adtr20,
    )


def entry_stop(
    playbook: Playbook,
    daily: DailyInputs,
    entry: Decimal,
    day_lows: Sequence[Decimal],
    final_contraction_low: Decimal | None,
) -> StopPlan:
    """The initial stop at a trigger (HAT-BOT-062), live and in the backtest: the playbook's rule
    on the entry, the day's lows up to the trigger, the setup's contraction low and `daily`'s
    tick, ATR and swing lows; without a tick it skips (`stop_input_missing`)."""
    if daily.tick is None:
        return StopPlan(None, SKIP_BAD_INPUT)
    return plan_stop(
        playbook.stop,
        StopInputs(
            entry=entry,
            tick=daily.tick,
            final_contraction_low=final_contraction_low,
            day_lows=tuple(day_lows),
            atr=daily.atr,
            swing_lows=daily.swing_lows,
        ),
    )


def entry_filter_context(
    ctx: LiveContext, *, entry: Decimal, stop: Decimal | None, level: Decimal, locked: bool | None
) -> LiveContext:
    """The context the entry filters read at a trigger, live and in the backtest: the trigger's
    context `ctx` (`daily_context`, plus the bar's values live) at the entry price, with its
    stop, the trigger level and the upper-circuit state."""
    return replace(ctx, price=entry).for_entry(
        entry=entry,
        stop=stop,
        adtr20=ctx.adtr20,
        trigger_level=level,
        locked_upper_circuit=locked,
    )


def evaluate_entry_filters(
    playbook: Playbook,
    ctx: LiveContext,
    *,
    configured_pct: Decimal | None,
    strategy_key: str,
) -> tuple[str | None, tuple[CriterionOutcome, ...]]:
    """(the type of the first failing entry filter or None, every filter's outcome) for the
    playbook's entry filters at a trigger (`entry_filter_instances`), live and in the backtest."""
    outcomes: list[CriterionOutcome] = []
    failed = None
    for instance in entry_filter_instances(playbook, configured_pct, strategy_key):
        result = evaluate_live(instance.type, instance.params, ctx)
        outcomes.append(CriterionOutcome(instance.type, instance.order, True, result))
        if failed is None and not result.passed:
            failed = instance.type
    return failed, tuple(outcomes)


def in_window(at: datetime, opens: datetime, closes: datetime) -> bool:
    """Whether a trigger may fire at `at` in a session open from `opens` to `closes`: from 5
    minutes after the open to 10 minutes before continuous trading ends (`CLOSING_AUCTION` before
    the close), both inclusive ([09:20, 15:05] IST in a regular session, HAT-EXEC-120)."""
    first = opens + TRIGGER_WINDOW[0]
    last = closes - CLOSING_AUCTION - TRIGGER_WINDOW[1]
    return first <= at <= last


# --- the monitor ------------------------------------------------------------------------------


class TriggerMonitor:
    """Watches the day's candidates and fires each until its signal is written. `on_bar` is a
    bar listener of `LiveData`, called on the loop thread with each final bar in order;
    `confirm` and `release` may come from the worker that wrote the signal, so the fired and
    pending sets are guarded by a lock."""

    def __init__(self, view: BarView) -> None:
        self._view = view
        self._by_symbol: dict[str, dict[tuple[uuid.UUID, str, uuid.UUID, date], Candidate]] = {}
        self._fired: set[tuple[uuid.UUID, str, uuid.UUID, date]] = set()
        self._pending: set[tuple[uuid.UUID, str, uuid.UUID, date]] = set()
        # per symbol, the end of its latest final bar: a trade before it is a late print
        self._final_until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def add(self, candidate: Candidate) -> None:
        """Watch `candidate` (a second add of the same key replaces the first)."""
        self._by_symbol.setdefault(candidate.symbol, {})[candidate.key] = candidate

    def mark_fired(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> None:
        """A key that already has its signal (a restart mid-session) never fires again."""
        with self._lock:
            self._fired.add(key)

    def confirm(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> None:
        """The fired key's signal is stored: it never fires again that day."""
        with self._lock:
            self._pending.discard(key)
            self._fired.add(key)

    def release(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> None:
        """The fired key's signal write failed: the next final bar evaluates it again."""
        with self._lock:
            self._pending.discard(key)

    def _claim(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> bool:
        with self._lock:
            if key in self._fired or key in self._pending:
                return False
            self._pending.add(key)
            return True

    def _open(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> bool:
        with self._lock:
            return key not in self._fired and key not in self._pending

    def candidates(self) -> list[Candidate]:
        return [c for group in self._by_symbol.values() for c in group.values()]

    def context(self, candidate: Candidate, bar: Bar) -> LiveContext:
        """The live context of `candidate` at the final instant of `bar`."""
        day = candidate.trade_date
        opening = self._view.opening_range(candidate.symbol, day)
        levels = {
            **candidate.levels,
            LEVEL_OPENING_RANGE: None if opening is None else opening[0],
        }
        return replace(
            daily_context(bar.final_at, candidate.daily, levels),
            price=bar.close,
            bar_high=bar.high,
            bar_close=bar.close,
            cum_volume=self._view.day_volume(candidate.symbol, day),
            # the instant the day volume is read, against the day's own session (Muhurat)
            elapsed=elapsed_fraction(
                bar.final_at,
                session_open=self._view.session_start(day).astimezone(IST).time(),
                session_close=self._view.session_end(day).astimezone(IST).time(),
            ),
            open_price=candidate.open_price,
            announcements=candidate.announcements,
            minutes=minutes_since_open(bar, self._view.session_start(day)),
            results=candidate.results,
            results_since=candidate.results_since,
        )

    def tick_context(self, candidate: Candidate, tick: Tick) -> LiveContext:
        """The live context of `candidate` at a trade (`on_tick`): the trade is the bar (price,
        high and close), the day volume that of the final bars so far."""
        day = candidate.trade_date
        start = self._view.session_start(day)
        return replace(
            daily_context(tick.at, candidate.daily, candidate.levels),
            price=tick.price,
            bar_high=tick.price,
            bar_close=tick.price,
            cum_volume=self._view.day_volume(candidate.symbol, day),
            elapsed=elapsed_fraction(
                tick.at,
                session_open=start.astimezone(IST).time(),
                session_close=self._view.session_end(day).astimezone(IST).time(),
            ),
            open_price=candidate.open_price,
            announcements=candidate.announcements,
            minutes=Fraction(int((tick.at - start).total_seconds()), 60),
            results=candidate.results,
            results_since=candidate.results_since,
        )

    def on_tick(self, tick: Tick) -> list[Trigger]:
        """Evaluate every unfired candidate of the trade's symbol that fires on a trade
        (`Playbook.on_ticks`) at the trade's exchange time; return the triggers fired. The day
        low of a stop plan is the final bars' so far. A late print, timed inside a bar already
        final (`on_bar`), fires nothing: its price is no longer the market's."""
        day = tick.at.astimezone(IST).date()
        if not in_window(tick.at, self._view.session_start(day), self._view.session_end(day)):
            return []
        until = self._final_until.get(tick.symbol)
        if until is not None and tick.at < until:
            return []
        fired: list[Trigger] = []
        for key, candidate in sorted(
            self._by_symbol.get(tick.symbol, {}).items(), key=lambda kv: str(kv[0])
        ):
            if not candidate.playbook.on_ticks or candidate.trade_date != day:
                continue
            if not self._open(key):
                continue
            ctx = self.tick_context(candidate, tick)
            evaluation = evaluate_trigger(
                candidate.playbook, ctx, candidate.mode, candidate.daily_results
            )
            if not evaluation.fired or not self._claim(key):
                continue
            low = self._view.day_low(tick.symbol, day)
            plan = plan_entry(candidate, ctx, evaluation.level, () if low is None else (low,))
            fired.append(Trigger(candidate, tick.at, evaluation, plan, ctx))
        return fired

    def on_bar(self, bar: Bar) -> list[Trigger]:
        """Evaluate every unfired candidate of the bar's symbol; return the triggers fired. A
        candidate that fires on a trade (`on_tick`) is not evaluated here."""
        day = bar.start.astimezone(IST).date()
        end = bar.start + BAR_WIDTH
        if end > self._final_until.get(bar.symbol, end - BAR_WIDTH):
            self._final_until[bar.symbol] = end
        if not in_window(bar.final_at, self._view.session_start(day), self._view.session_end(day)):
            return []
        fired: list[Trigger] = []
        for key, candidate in sorted(
            self._by_symbol.get(bar.symbol, {}).items(), key=lambda kv: str(kv[0])
        ):
            if candidate.playbook.on_ticks:
                continue
            if candidate.trade_date != day or not self._open(key):
                continue
            ctx = self.context(candidate, bar)
            evaluation = evaluate_trigger(
                candidate.playbook, ctx, candidate.mode, candidate.daily_results
            )
            if not evaluation.fired or not self._claim(key):
                continue
            low = self._view.day_low(bar.symbol, day, bar.start)
            plan = plan_entry(candidate, ctx, evaluation.level, () if low is None else (low,))
            fired.append(Trigger(candidate, bar.final_at, evaluation, plan, ctx))
        return fired
