"""The latest watchlist of each bot on or before a day, one row per symbol at its best score."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.instrument_names import instrument_symbols
from ..models import HatWatchlistEntry

__all__ = ["Watchlist", "latest_watchlists"]


@dataclass(frozen=True)
class Watchlist:
    trade_date: date
    entries: tuple[tuple[str, Decimal], ...]  # (symbol, best score), best first, then symbol


def latest_watchlists(
    session: Session, on_or_before: date, bot_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, Watchlist]:
    """Each bot's (every bot's, or those of `bot_ids`) latest watchlist on or before
    `on_or_before`; a bot with none is left out."""
    latest = select(
        HatWatchlistEntry.bot_id, func.max(HatWatchlistEntry.trade_date).label("day")
    ).where(HatWatchlistEntry.trade_date <= on_or_before)
    if bot_ids is not None:
        latest = latest.where(HatWatchlistEntry.bot_id.in_(set(bot_ids)))
    latest = latest.group_by(HatWatchlistEntry.bot_id).subquery()
    days: dict[uuid.UUID, date] = {}
    best: dict[uuid.UUID, dict[str, Decimal]] = {}
    rows = session.execute(
        select(
            latest.c.bot_id, latest.c.day, HatWatchlistEntry.instrument_id, HatWatchlistEntry.score
        ).join(
            HatWatchlistEntry,
            (HatWatchlistEntry.bot_id == latest.c.bot_id)
            & (HatWatchlistEntry.trade_date == latest.c.day),
        )
    ).all()
    symbols = instrument_symbols(session, (instrument_id for _, _, instrument_id, _ in rows))
    for bot_id, day, instrument_id, score in rows:
        symbol = symbols.get(instrument_id)
        if symbol is None:
            continue
        days[bot_id] = day
        value = Decimal(score)
        scores = best.setdefault(bot_id, {})
        scores[symbol] = max(scores.get(symbol, value), value)
    return {
        bot_id: Watchlist(
            day, tuple(sorted(best[bot_id].items(), key=lambda item: (-item[1], item[0])))
        )
        for bot_id, day in days.items()
    }
