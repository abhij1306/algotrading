"""Builders for the live-trigger properties: playbooks, candidates, contexts and a tick-stream
driver that runs the real `BarBuilder` into the real `TriggerMonitor`.

Prices are integer paise turned into `Decimal`, so every oracle is exact. Time is explicit: the
driver passes each tick's own instant as its arrival time, and no property reads a clock."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import yaml

from app.hat.data.bars import Bar, BarBuilder, Tick
from app.hat.data.sessions import IST
from app.hat.engine.criteria import CriterionResult, evaluate_live
from app.hat.engine.criteria.live import LiveContext
from app.hat.engine.strategy.config import Playbook, StrategyConfig, parse_config
from app.hat.engine.strategy.versions import seed_text
from app.hat.engine.trigger import Candidate, DailyInputs, Trigger, TriggerMonitor

DAY = date(2026, 10, 21)
MODES = ("shadow", "confirm", "auto")
BOT = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
S1 = "stage2-contraction-breakout"
S2 = "momentum-breakout-ep"
FEED = "FEED"  # another symbol on the feed, never a candidate


def paise(value: int) -> Decimal:
    return Decimal(value).scaleb(-2)


def ist(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=IST)


AT = ist(DAY, 10, 32, 2)


def playbook(
    trigger: Sequence[dict[str, Any]] = (),
    filters: Sequence[dict[str, Any]] = (),
    stop: dict[str, Any] | None = None,
    name: str = "pb",
) -> Playbook:
    """A playbook from instance dicts (`stage` and `order` filled from the position)."""

    def fill(items: Sequence[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
        return [{"stage": stage, "order": i, **item} for i, item in enumerate(items)]

    return Playbook.model_validate(
        {
            "name": name,
            "setup": {"criteria": []},
            "trigger": fill(trigger, "trigger"),
            "entry_filters": fill(filters, "entry_filter"),
            "stop": stop or {"rule": "pct_below_entry", "pct": 5.0},
        }
    )


def seed_config(name: str) -> StrategyConfig:
    return parse_config(yaml.safe_load(seed_text(name)))


def seed_playbook(name: str, book: str) -> Playbook:
    return seed_config(name).playbook(book)


def candidate(
    book: Playbook,
    *,
    symbol: str = "AAA",
    mode: str = "shadow",
    strategy_key: str = S1,
    daily: DailyInputs | None = None,
    pivot: Decimal | None = None,
    consolidation_high: Decimal | None = None,
    open_price: Decimal | None = None,
    announcements: tuple[datetime, ...] = (),
    configured_pct: Decimal | None = None,
    instrument_id: uuid.UUID | None = None,
    bot_id: uuid.UUID = BOT,
    trade_date: date = DAY,
) -> Candidate:
    return Candidate(
        bot_id=bot_id,
        strategy_key=strategy_key,
        mode=mode,
        playbook=book,
        instrument_id=instrument_id or uuid.uuid5(uuid.NAMESPACE_URL, symbol),
        symbol=symbol,
        trade_date=trade_date,
        daily=daily or DailyInputs(tick=Decimal("0.05"), band_known=True, band_pct=None),
        levels={"pivot": pivot, "consolidation_high": consolidation_high},
        open_price=open_price,
        announcements=announcements,
        configured_max_chase_pct=configured_pct,
    )


def live_contract(code: str, params: dict[str, Any], ctx: LiveContext) -> CriterionResult:
    """The common contract for a live criterion: repeated calls agree, the score is in
    [0, 1] and `passed` is exactly reason `ok`."""
    first = evaluate_live(code, params, ctx)
    assert evaluate_live(code, params, ctx) == first
    assert 0.0 <= first.score <= 1.0
    assert first.passed == (first.reason == "ok")
    return first


def watch_from_open(builder: BarBuilder, symbols: Iterable[str], day: date = DAY) -> None:
    """A pre-open tick (09:00) for each symbol: the builder has watched it from the open."""
    preopen = ist(day, 9, 0)
    for symbol in symbols:
        builder.on_tick(Tick(symbol, preopen, Decimal(1)), preopen)


def drive(
    monitor: TriggerMonitor,
    builder: BarBuilder,
    ticks: Iterable[Tick],
    until: datetime,
    *,
    from_open: bool = True,
    seed: Callable[[Tick], tuple[Decimal, Decimal]] | None = None,
) -> tuple[list[Bar], list[Trigger]]:
    """Feed `ticks` in exchange-time order (each arriving at its own instant, then the bars
    final by then are handed to the monitor, as the live step does), and finish at `until`, when
    another symbol of the feed ticks. With `from_open` each symbol is first watched from before
    the open; with `seed` each tick also carries the exchange's (high, low) so far.
    Returns the final bars and the triggers, in order."""
    bars: list[Bar] = []
    triggers: list[Trigger] = []
    ticks = list(ticks)
    if from_open and ticks:
        watch_from_open(builder, {t.symbol for t in ticks}, ticks[0].at.astimezone(IST).date())

    def advance(now: datetime) -> None:
        for bar in builder.advance(now):
            bars.append(bar)
            triggers.extend(monitor.on_bar(bar))

    for tick in sorted(ticks, key=lambda t: t.at):
        builder.on_tick(tick, tick.at)
        if seed is not None:
            builder.seed(tick, *seed(tick), tick.at)
        advance(tick.at)
    builder.on_tick(Tick(FEED, until, Decimal(1)), until)  # the rest of the feed ticks on
    advance(until)
    return bars, triggers


def session_ticks(
    symbol: str, steps: Sequence[tuple[int, int, int]], day: date = DAY
) -> list[Tick]:
    """Ticks from (seconds after 09:15, price paise, qty) steps."""
    opens = ist(day, 9, 15)
    return [Tick(symbol, opens + timedelta(seconds=s), paise(p), q) for s, p, q in steps]
