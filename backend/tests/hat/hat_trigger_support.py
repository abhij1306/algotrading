"""Builders for the live-trigger properties: playbooks, candidates, contexts and a tick-stream
driver that runs the real `BarBuilder` into the real `TriggerMonitor`.

Prices are integer paise turned into `Decimal`, so every oracle is exact. Time is explicit: the
driver passes each tick's own instant as its arrival time, and no property reads a clock."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import yaml

from app.hat.data.bars import BarBuilder, Tick
from app.hat.engine.strategy.config import Playbook, StrategyConfig, parse_config
from app.hat.engine.strategy.versions import seed_text
from app.hat.engine.trigger import Candidate, DailyInputs
from app.hat.time import IST

DAY = date(2026, 10, 21)
BOT = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
S1 = "stage2-contraction-breakout"
S2 = "momentum-breakout-ep"


def paise(value: int) -> Decimal:
    return Decimal(value).scaleb(-2)


def ist(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=IST)


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


def watch_from_open(builder: BarBuilder, symbols: Iterable[str], day: date = DAY) -> None:
    """A pre-open tick (09:00) for each symbol: the builder has watched it from the open."""
    preopen = ist(day, 9, 0)
    for symbol in symbols:
        builder.on_tick(Tick(symbol, preopen, Decimal(1)), preopen)
