"""Read-only queries behind the Telegram commands and reports (08 §2, §3; HAT-TG-022, -024,
-037). Nothing here writes: `/status`, `/positions`, `/watchlist` and `/login` change no table
(HAT-TG-037), and the scheduled reports only read what they summarise.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.calendar import MarketCalendar, load_calendar, trading_days
from ..models import (
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatDailyBotSnapshot,
    HatEvent,
    HatInstrument,
    HatPosition,
    HatRegimeState,
    HatSignal,
    HatStrategy,
    HatWatchlistEntry,
)
from ..risk import controls, kill
from ..time import as_utc, day_bounds, trade_date_of
from . import render

__all__ = [
    "ACTIVE",
    "LIVE",
    "LoginLinks",
    "PriceSource",
    "accounts",
    "bot_label",
    "bots_named",
    "daily_bots",
    "logged_out",
    "position_rows",
    "rejection_rows",
    "status_view",
    "symbol_of",
    "watchlist_digest",
    "watchlist_rows",
]

ACTIVE = "active"
LIVE = "live"
TOP_N = 10
_ID_PREFIX = 6

# The live price of an instrument (the feed's last trade), None when unknown.
PriceSource = Callable[[uuid.UUID], Decimal | None]


class LoginLinks(Protocol):
    def link(self, account: HatBrokerAccount) -> str | None:
        """A fresh login link for the account's broker, None when one cannot be made."""
        ...


# --- bots and instruments ----------------------------------------------------------------------


def bot_label(session: Session, bot: HatBot) -> str:
    name = session.scalar(select(HatStrategy.name).where(HatStrategy.id == bot.strategy_id))
    return name or str(bot.id)[:8]


def bots_named(session: Session, name: str | None) -> list[HatBot]:
    """Every bot when `name` is None; else the bots whose strategy name is `name` (any case) or
    whose id starts with it (6 or more characters). Ordered by id."""
    bots = list(session.scalars(select(HatBot).order_by(HatBot.id)))
    if name is None:
        return bots
    wanted = name.strip().lower()
    return [
        b
        for b in bots
        if bot_label(session, b).lower() == wanted
        or (len(wanted) >= _ID_PREFIX and str(b.id).startswith(wanted))
    ]


def symbol_of(session: Session, instrument_id: uuid.UUID) -> str:
    symbol = session.scalar(select(HatInstrument.symbol).where(HatInstrument.id == instrument_id))
    return symbol or "?"


# --- accounts and broker sessions --------------------------------------------------------------


def accounts(session: Session, broker: str | None = None) -> list[HatBrokerAccount]:
    """The active broker accounts (of one broker, any case), ordered by id."""
    query = select(HatBrokerAccount).where(HatBrokerAccount.status == ACTIVE)
    rows = list(session.scalars(query.order_by(HatBrokerAccount.id)))
    if broker is None:
        return rows
    return [a for a in rows if a.broker.lower() == broker.strip().lower()]


def session_status(session: Session, account_id: uuid.UUID, day: date) -> str:
    row = session.get(HatBrokerSession, (account_id, day))
    return row.status if row is not None else "missing"


def logged_out(session: Session, day: date) -> list[HatBrokerAccount]:
    """The active accounts with no live `hat_broker_session` for `day` (the 09:05 prompt)."""
    return [a for a in accounts(session) if session_status(session, a.id, day) != LIVE]


def account_label(account: HatBrokerAccount) -> str:
    return f"{account.broker} {account.account_ref}"


# --- /status -----------------------------------------------------------------------------------


def _regime(session: Session) -> str:
    row = session.scalars(
        select(HatRegimeState).order_by(HatRegimeState.trade_date.desc()).limit(1)
    ).first()
    if row is None:
        return "n/a"
    return f"{row.state}{'' if row.confirmed else ' (unconfirmed)'} {row.trade_date.isoformat()}"


def _bot_state(session: Session, bot: HatBot) -> str:
    state = controls.kill_state(session, bot.id)
    reasons = [
        name
        for name, on in (
            ("account kill", state.account_tripped),
            ("bot kill", state.bot_tripped),
            ("health kill", state.health_tripped),
            ("paused", state.paused),
        )
        if on
    ]
    return "manage-only: " + ", ".join(reasons) if reasons else "active"


def status_view(session: Session, now: datetime, feed: str) -> render.StatusView:
    day = trade_date_of(now)
    regime = _regime(session)
    rows = controls.kill_rows(session)
    account_tripped = any(
        kill.current_state(rows, "account", None, channel) == kill.TRIPPED
        for channel in ("risk", "health")
    )
    return render.StatusView(
        sessions=tuple(
            (account_label(a), session_status(session, a.id, day)) for a in accounts(session)
        ),
        feed=feed,
        kill_account=account_tripped,
        bots=tuple(
            (bot_label(session, b), b.execution_mode, _bot_state(session, b), regime)
            for b in bots_named(session, None)
        ),
    )


# --- /positions --------------------------------------------------------------------------------


def _days_held(calendar: MarketCalendar | None, opened: datetime, now: datetime) -> int | None:
    if calendar is None:
        return None
    start, end = trade_date_of(opened), trade_date_of(now)
    if end < start:
        return 0
    return max(len(trading_days(calendar, start, end)) - 1, 0)


def r_so_far(entry: Decimal, initial_stop: Decimal, price: Decimal | None) -> Decimal | None:
    """(price - entry) / (entry - initial stop); None without a price or a positive risk."""
    risk = entry - initial_stop
    if price is None or risk <= 0:
        return None
    return (price - entry) / risk


def position_rows(session: Session, now: datetime, price: PriceSource) -> list[render.PositionView]:
    rows = []
    calendar = load_calendar(session)  # once per command, not once per position
    for p in session.scalars(
        select(HatPosition)
        .where(HatPosition.closed_at.is_(None), HatPosition.qty_open > 0)
        .order_by(HatPosition.opened_at, HatPosition.id)
    ):
        bot = session.get(HatBot, p.bot_id)
        entry, stop = Decimal(p.avg_entry), Decimal(p.current_stop)
        rows.append(
            render.PositionView(
                bot=bot_label(session, bot) if bot is not None else "?",
                symbol=symbol_of(session, p.instrument_id),
                qty=p.qty_open,
                entry=entry,
                stop=stop,
                r_so_far=r_so_far(entry, Decimal(p.initial_stop), price(p.instrument_id)),
                days_held=_days_held(calendar, p.opened_at, now),
                virtual=bool(p.is_virtual),
            )
        )
    return rows


# --- watchlists --------------------------------------------------------------------------------


def _watchlist_day(session: Session, bot_id: uuid.UUID, on_or_before: date) -> date | None:
    return session.scalar(
        select(func.max(HatWatchlistEntry.trade_date)).where(
            HatWatchlistEntry.bot_id == bot_id, HatWatchlistEntry.trade_date <= on_or_before
        )
    )


def _entries(session: Session, bot_id: uuid.UUID, day: date | None) -> list[tuple[str, Decimal]]:
    """(symbol, score) of a bot's watchlist on `day`, best first (score, then symbol)."""
    if day is None:
        return []
    rows = session.execute(
        select(HatInstrument.symbol, HatWatchlistEntry.score)
        .join(HatInstrument, HatInstrument.id == HatWatchlistEntry.instrument_id)
        .where(HatWatchlistEntry.bot_id == bot_id, HatWatchlistEntry.trade_date == day)
    ).all()
    best: dict[str, Decimal] = {}
    for symbol, score in rows:
        value = Decimal(score)
        best[symbol] = max(best.get(symbol, value), value)
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))


def watchlist_rows(
    session: Session, bot: HatBot, day: date
) -> tuple[date | None, list[tuple[str, Decimal]]]:
    """The bot's latest watchlist on or before `day`."""
    found = _watchlist_day(session, bot.id, day)
    return found, _entries(session, bot.id, found)


def watchlist_digest(session: Session, bot: HatBot, day: date) -> render.WatchlistDigest:
    """HAT-TG-022: new = today minus the previous watchlist, dropped = the previous minus today,
    top = the first min(10, n) of today by score descending."""
    today = _entries(session, bot.id, day)
    previous = _entries(session, bot.id, _watchlist_day(session, bot.id, day - timedelta(days=1)))
    now_set = {s for s, _ in today}
    before = {s for s, _ in previous}
    return render.WatchlistDigest(
        bot=bot_label(session, bot),
        trade_date=day,
        regime=_regime(session),
        new=tuple(sorted(now_set - before)),
        dropped=tuple(sorted(before - now_set)),
        top=tuple(today[:TOP_N]),
    )


# --- reports -----------------------------------------------------------------------------------


def rejection_rows(session: Session, day: date) -> list[render.RejectionRow]:
    """HAT-TG-024, HAT-EXEC-043: every `signal.rejected` event of the trade date, in order."""
    start, end = day_bounds(day)
    rows = []
    for event in session.scalars(
        select(HatEvent)
        .where(
            HatEvent.event_type == "signal.rejected",
            HatEvent.occurred_at >= start,
            HatEvent.occurred_at < end,
        )
        .order_by(HatEvent.id)
    ):
        signal = session.get(HatSignal, event.aggregate_id)
        bot = session.get(HatBot, signal.bot_id) if signal is not None else None
        rows.append(
            render.RejectionRow(
                bot=bot_label(session, bot) if bot is not None else "?",
                symbol=symbol_of(session, signal.instrument_id) if signal is not None else "?",
                reason=str((event.payload or {}).get("reason") or "unknown"),
            )
        )
    return rows


def daily_bots(session: Session, day: date) -> list[render.BotDay]:
    """HAT-TG-024: per bot, its latest `hat_daily_bot_snapshot` on or before `day`."""
    out = []
    for bot in bots_named(session, None):
        snap = session.scalars(
            select(HatDailyBotSnapshot)
            .where(HatDailyBotSnapshot.bot_id == bot.id, HatDailyBotSnapshot.trade_date <= day)
            .order_by(HatDailyBotSnapshot.trade_date.desc())
            .limit(1)
        ).first()
        out.append(
            render.BotDay(
                bot=bot_label(session, bot),
                trade_date=snap.trade_date if snap else None,
                open_risk=Decimal(snap.open_risk) if snap else None,
                realized_r=Decimal(snap.realized_r) if snap else None,
                equity=Decimal(snap.equity) if snap else None,
                equity_peak=Decimal(snap.equity_peak) if snap else None,
            )
        )
    return out


def events_between(
    session: Session, types: Sequence[str], start: datetime, end: datetime
) -> list[HatEvent]:
    return list(
        session.scalars(
            select(HatEvent)
            .where(
                HatEvent.event_type.in_(tuple(types)),
                HatEvent.occurred_at >= as_utc(start),
                HatEvent.occurred_at < as_utc(end),
            )
            .order_by(HatEvent.id)
        )
    )
