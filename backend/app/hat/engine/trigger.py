"""The trigger monitor: live trigger and entry-filter evaluation on final 5-minute bars (07 §12,
HAT-EXEC-120..122, HAT-CRIT-115..122, HAT-BOT-052, HAT-BOT-071).

Pure and synchronous, like the bar builder it listens to: `TriggerMonitor.on_bar` takes one final
bar and returns the triggers it fired, and reads only the candidates it was given and the bar
view (opening range, day volume). Nothing here reads a clock, the database or the network;
`live_entry.py` loads the candidates and turns a trigger into a signal.

Rules:

- A trigger is evaluated only on a final bar (the builder hands over a bar at its end + 2 s),
  and only when that instant lies in [09:20, 15:20] IST; the trigger's time is the bar's final
  instant (HAT-EXEC-120).
- A playbook triggers when every enabled trigger criterion passes on the same bar (HAT-EXEC-121).
  A trigger type over daily bars (`neglect`) is evaluated once, on the bars before the trade
  date, when the candidate is built (`daily_trigger_results`); without that result it fails
  closed.
  `catalyst_attached` is the one exception: its result is recorded, but it blocks only when the
  bot's mode is in its `required_in` list (HAT-CRIT-118, HAT-EXEC-034). A playbook with no
  enabled trigger criterion never triggers.
- `price_breaks_level` breaks when the bar's high is strictly above the level; the trigger price
  is the level. The level of an opening-range expression does not exist before the first bar is
  final, so nothing fires before 09:20 (HAT-CRIT-119, HAT-BOT-071).
- At the trigger the initial stop is computed with the instrument's real tick (HAT-EXEC-053),
  then the entry filters run in their configured order; `max_chase` always runs, with the
  playbook's last configured percentage, even when the config deleted or disabled it
  (HAT-CRIT-122). The first failure names the skip reason (HAT-EXEC-122).
- One (bot, playbook, instrument, trade date) fires at most once.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Protocol

import polars as pl

from ..data.bars import Bar
from ..data.sessions import IST, SESSION_CLOSE, SESSION_OPEN
from ..risk.caps import effective_max_chase_pct
from .criteria import CriterionResult, EvalContext, evaluate, evaluate_live, get_type
from .criteria.live import CONSOLIDATION_HIGH, LEVEL_OPENING_RANGE, LEVEL_PIVOT, LiveContext
from .stops import StopInputs, floor_to_tick, plan_stop
from .strategy.config import Instance, Playbook, StrategyConfig

__all__ = [
    "TRIGGER_WINDOW",
    "BarView",
    "Candidate",
    "CriterionOutcome",
    "DailyInputs",
    "EntryPlan",
    "Trigger",
    "TriggerEvaluation",
    "TriggerMonitor",
    "daily_inputs",
    "daily_trigger_results",
    "elapsed_fraction",
    "entry_filter_instances",
    "evaluate_trigger",
    "last_configured_chase",
    "locked_upper_circuit",
    "plan_entry",
    "upper_circuit_price",
]

TRIGGER_WINDOW = (time(9, 20), time(15, 20))
MAX_CHASE = "max_chase"
CATALYST = "catalyst_attached"
PRICE_BREAKS_LEVEL = "price_breaks_level"
SKIP_NO_LEVEL = "level_undefined"
SKIP_NO_PRICE = "no_live_price"
AVG_VOLUME_DAYS = 50
_HUNDRED = Decimal(100)


class BarView(Protocol):
    """What the monitor reads from the bar builder besides the bar itself."""

    def opening_range(self, symbol: str, day: date) -> tuple[Decimal, Decimal] | None: ...

    def day_volume(self, symbol: str, day: date) -> int: ...


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


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    number = value if isinstance(value, Decimal) else Decimal(repr(value))
    return number if number.is_finite() else None


def daily_inputs(
    bars: pl.DataFrame,
    trade_date: date,
    *,
    tick: Decimal | None,
    adv20: Decimal | None,
    adtr20: Decimal | None,
    band_known: bool,
    band_pct: Decimal | None,
    swing_lookback: int = 0,
    atr: Decimal | None = None,
) -> DailyInputs:
    """`DailyInputs` from one instrument's adjusted daily bars (`date`, `low`, `close`,
    `volume`): the previous close is the last close before `trade_date` (HAT-CRIT-115), the
    average volume the mean of the last 50 sessions' volume before it (HAT-DATA-115; None with
    fewer than 50), and the swing lows the last `swing_lookback` lows (none with fewer).
    `tick` comes from `data.ticks` on the unadjusted closes."""
    prior = bars.filter(pl.col("date") < trade_date).sort("date")
    closes = [_dec(v) for v in prior["close"].to_list()] if prior.height else []
    volumes = [_dec(v) for v in prior["volume"].to_list()] if prior.height else []
    lows = [_dec(v) for v in prior["low"].to_list()] if prior.height else []
    recent = volumes[-AVG_VOLUME_DAYS:]
    avg50 = (
        sum((v for v in recent if v is not None), Decimal(0)) / AVG_VOLUME_DAYS
        if len(recent) == AVG_VOLUME_DAYS and all(v is not None for v in recent)
        else None
    )
    swing = lows[-swing_lookback:] if swing_lookback and len(lows) >= swing_lookback else []
    return DailyInputs(
        prev_close=closes[-1] if closes else None,
        avg50_volume=avg50,
        adv20=adv20,
        adtr20=adtr20,
        tick=tick,
        band_known=band_known,
        band_pct=band_pct,
        atr=atr,
        swing_lows=tuple(v for v in swing if v is not None),
    )


@dataclass(frozen=True)
class Candidate:
    """One (bot, playbook, instrument) the monitor watches on `trade_date`.

    `levels` holds the watchlist's `pivot` and `consolidation_high`; `open_price` is the day's
    open from the pre-open capture (EP); `announcements` are the `published_at` instants matched
    to the instrument. `configured_max_chase_pct` is the playbook's last configured max-chase
    percentage across the bot's versions (None when it never had one). `daily_results` holds the
    results of the playbook's daily-bar trigger criteria by instance order."""

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


def last_configured_chase(configs: Sequence[StrategyConfig], playbook: str) -> Decimal | None:
    """The playbook's last configured max-chase percentage, newest config first: the first
    config whose playbook holds a `max_chase` instance (enabled or not) gives it."""
    for config in configs:
        for book in config.playbooks:
            if book.name != playbook:
                continue
            for instance in book.entry_filters:
                if instance.type == MAX_CHASE:
                    return Decimal(repr(instance.params["pct"]))
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
    forced = Instance(
        type=MAX_CHASE,
        stage="entry_filter",
        params={"pct": float(pct)},
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
        if instance.type == PRICE_BREAKS_LEVEL and result.passed:
            level = result.extras.get("level")
    blocking_results = [o.result.passed for o in outcomes if o.blocking]
    fired = bool(blocking_results) and all(blocking_results)
    return TriggerEvaluation(fired, tuple(outcomes), level)


def plan_entry(
    candidate: Candidate,
    ctx: LiveContext,
    level: Decimal | None,
    day_lows: Sequence[Decimal] = (),
) -> EntryPlan:
    """The stop and the entry filters at the trigger. `ctx.price` is the entry price and
    `day_lows` the lows of the day's final bars up to and including the trigger bar."""
    entry = ctx.price
    if entry is None or entry <= 0:
        return EntryPlan(None, level, None, None, SKIP_NO_PRICE, ())
    locked = locked_upper_circuit(entry, candidate.daily)
    if level is None:
        return EntryPlan(entry, None, None, locked, SKIP_NO_LEVEL, ())
    daily = candidate.daily
    stop = None
    skip = None
    if daily.tick is None:
        skip = "stop_input_missing"
    else:
        planned = plan_stop(
            candidate.playbook.stop,
            StopInputs(
                entry=entry,
                tick=daily.tick,
                final_contraction_low=candidate.final_contraction_low,
                day_lows=tuple(day_lows),
                atr=daily.atr,
                swing_lows=daily.swing_lows,
            ),
        )
        stop, skip = planned.stop, planned.skip
    filter_ctx = ctx.for_entry(
        entry=entry,
        stop=stop,
        adtr20=daily.adtr20,
        trigger_level=level,
        locked_upper_circuit=locked,
    )
    outcomes: list[CriterionOutcome] = []
    for instance in entry_filter_instances(
        candidate.playbook, candidate.configured_max_chase_pct, candidate.strategy_key
    ):
        result = evaluate_live(instance.type, instance.params, filter_ctx)
        outcomes.append(CriterionOutcome(instance.type, instance.order, True, result))
        if skip is None and not result.passed:
            skip = instance.type
    return EntryPlan(entry, level, stop, locked, skip, tuple(outcomes))


def in_window(at: datetime) -> bool:
    """Whether a trigger may fire at `at`: inside [09:20, 15:20] IST (HAT-EXEC-120)."""
    local = at.astimezone(IST).time()
    return TRIGGER_WINDOW[0] <= local <= TRIGGER_WINDOW[1]


# --- the monitor ------------------------------------------------------------------------------


class TriggerMonitor:
    """Watches the day's candidates and fires each at most once. Single-threaded by contract:
    it is a bar listener of `LiveData`, called on the loop thread with each final bar in order."""

    def __init__(self, view: BarView) -> None:
        self._view = view
        self._by_symbol: dict[str, dict[tuple[uuid.UUID, str, uuid.UUID, date], Candidate]] = {}
        self._fired: set[tuple[uuid.UUID, str, uuid.UUID, date]] = set()
        self._day_lows: dict[tuple[str, date], list[Decimal]] = {}

    def add(self, candidate: Candidate) -> None:
        """Watch `candidate` (a second add of the same key replaces the first)."""
        self._by_symbol.setdefault(candidate.symbol, {})[candidate.key] = candidate

    def mark_fired(self, key: tuple[uuid.UUID, str, uuid.UUID, date]) -> None:
        """A key that already has its signal (a restart mid-session) never fires again."""
        self._fired.add(key)

    def candidates(self) -> list[Candidate]:
        return [c for group in self._by_symbol.values() for c in group.values()]

    def context(self, candidate: Candidate, bar: Bar) -> LiveContext:
        """The live context of `candidate` at the final instant of `bar`."""
        day = candidate.trade_date
        opening = self._view.opening_range(candidate.symbol, day)
        levels = {
            LEVEL_PIVOT: candidate.levels.get(LEVEL_PIVOT),
            CONSOLIDATION_HIGH: candidate.levels.get(CONSOLIDATION_HIGH),
            LEVEL_OPENING_RANGE: None if opening is None else opening[0],
        }
        daily = candidate.daily
        return LiveContext(
            at=bar.final_at,
            price=bar.close,
            bar_high=bar.high,
            levels=levels,
            cum_volume=self._view.day_volume(candidate.symbol, day),
            elapsed=elapsed_fraction(bar.final_at),  # the instant the day volume is read
            avg50_volume=daily.avg50_volume,
            adv20=daily.adv20,
            open_price=candidate.open_price,
            prev_close=daily.prev_close,
            announcements=candidate.announcements,
            adtr20=daily.adtr20,
        )

    def on_bar(self, bar: Bar) -> list[Trigger]:
        """Evaluate every unfired candidate of the bar's symbol; return the triggers fired."""
        day = bar.start.astimezone(IST).date()
        self._day_lows.setdefault((bar.symbol, day), []).append(bar.low)
        if not in_window(bar.final_at):
            return []
        fired: list[Trigger] = []
        for key, candidate in sorted(
            self._by_symbol.get(bar.symbol, {}).items(), key=lambda kv: str(kv[0])
        ):
            if key in self._fired or candidate.trade_date != day:
                continue
            ctx = self.context(candidate, bar)
            evaluation = evaluate_trigger(
                candidate.playbook, ctx, candidate.mode, candidate.daily_results
            )
            if not evaluation.fired:
                continue
            self._fired.add(key)
            lows = self._day_lows[(bar.symbol, day)]
            plan = plan_entry(candidate, ctx, evaluation.level, lows)
            fired.append(Trigger(candidate, bar.final_at, evaluation, plan, ctx))
        return fired
