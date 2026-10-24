"""Read-only queries behind the Telegram commands and reports (HAT-TG-022, -024,
-037). Nothing here writes: `/status`, `/positions`, `/watchlist` and `/login` change no table
(HAT-TG-037), and the scheduled reports only read what they summarise. Bots, kill switches, open
positions and watchlists come from the shared read model (`app/hat/read`, the dashboard's too);
this module adds only what Telegram shows beyond it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data.calendar import session_calendar
from ..models import (
    HatBot,
    HatBrokerAccount,
    HatDailyBotSnapshot,
    HatEvent,
    HatPosition,
    HatSignal,
)
from ..models.strategy import ACCOUNT_ACTIVE
from ..read import bots as read_bots
from ..read import kill as read_kill
from ..read.positions import UNKNOWN, open_positions, symbols
from ..read.regime import latest_regimes
from ..read.sessions import broker_sessions
from ..read.watchlist import latest_watchlists
from ..risk.sessions import SessionCalendar
from ..time import as_utc, day_bounds, trade_date_of
from . import render

__all__ = [
    "LIVE",
    "LoginLinks",
    "PriceSource",
    "accounts",
    "bots_named",
    "daily_bots",
    "logged_out",
    "position_rows",
    "rejection_rows",
    "status_view",
    "watchlist_digest",
    "watchlist_texts",
]

LIVE = "live"
TOP_N = 10
_ID_PREFIX = 6

# The live price of an instrument (the feed's last trade), None when unknown.
PriceSource = Callable[[uuid.UUID], Decimal | None]


class LoginLinks(Protocol):
    def link(self, account: HatBrokerAccount) -> str | None:
        """A fresh login link for the account's broker, None when one cannot be made."""
        ...


# --- bots --------------------------------------------------------------------------------------


def bots_named(session: Session, name: str | None) -> list[HatBot]:
    """Every bot when `name` is None; else the bots whose strategy name is `name` (any case) or
    whose id starts with it (6 or more characters). Ordered by id."""
    bots = read_bots.all_bots(session)
    if name is None:
        return bots
    wanted = name.strip().lower()
    labels = read_bots.labels(session, (b.id for b in bots))
    return [
        b
        for b in bots
        if labels[b.id].lower() == wanted
        or (len(wanted) >= _ID_PREFIX and str(b.id).startswith(wanted))
    ]


# --- accounts and broker sessions --------------------------------------------------------------


def accounts(session: Session, broker: str | None = None) -> list[HatBrokerAccount]:
    """The active broker accounts (of one broker, any case), ordered by id."""
    query = select(HatBrokerAccount).where(HatBrokerAccount.status == ACCOUNT_ACTIVE)
    rows = list(session.scalars(query.order_by(HatBrokerAccount.id)))
    if broker is None:
        return rows
    return [a for a in rows if a.broker.lower() == broker.strip().lower()]


def logged_out(session: Session, day: date) -> list[HatBrokerAccount]:
    """The active accounts with no live `hat_broker_session` for `day` (the 09:05 prompt)."""
    return [row.account for row in broker_sessions(session, day) if row.status != LIVE]


def open_real_positions(session: Session, account_ids: Sequence[uuid.UUID]) -> int:
    """The real open positions of the accounts' bots (the 09:05 prompt's escalation)."""
    if not account_ids:
        return 0
    bots = select(HatBot.id).where(HatBot.broker_account_id.in_(account_ids))
    return (
        session.scalar(
            select(func.count())
            .select_from(HatPosition)
            .where(
                HatPosition.bot_id.in_(bots),
                HatPosition.is_virtual.is_(False),
                HatPosition.closed_at.is_(None),
                HatPosition.qty_open > 0,
            )
        )
        or 0
    )


# --- /status -----------------------------------------------------------------------------------


def _regime(session: Session) -> str:
    rows = latest_regimes(session, 1)
    if not rows:
        return "n/a"
    row = rows[0]
    return f"{row.state}{'' if row.confirmed else ' (unconfirmed)'} {row.trade_date.isoformat()}"


def _bot_state(row: read_bots.BotRow) -> str:
    reasons = [reason.replace("_", " ") for reason in row.reasons]
    return "manage-only: " + ", ".join(reasons) if reasons else "active"


def _mode(row: read_bots.BotRow) -> str:
    """The effective and the asked mode and the queued change (HAT-TG-034, D130)."""
    view = row.mode
    return render.mode_text(view.asked, view.effective, view.why, view.pending, view.pending_from)


def status_view(session: Session, now: datetime, feed: str) -> render.StatusView:
    day = trade_date_of(now)
    regime = _regime(session)
    return render.StatusView(
        sessions=tuple((row.label, row.status) for row in broker_sessions(session, day)),
        feed=feed,
        kill_account=read_kill.account_tripped(read_kill.kill_rows(session)),
        bots=tuple(
            (row.label, _mode(row), _bot_state(row), regime)
            for row in read_bots.bot_rows(session, now)
        ),
    )


# --- /positions --------------------------------------------------------------------------------


def _days_held(calendar: SessionCalendar | None, opened: datetime, now: datetime) -> int | None:
    """Trading days from the open's to now's, the first not counted (a day the calendar does not
    hold is not one)."""
    if calendar is None:
        return None
    start, end = trade_date_of(opened), trade_date_of(now)
    return max(sum(1 for day in calendar.trading_days() if start <= day <= end) - 1, 0)


def r_so_far(entry: Decimal, initial_stop: Decimal, price: Decimal | None) -> Decimal | None:
    """(price - entry) / (entry - initial stop); None without a price or a positive risk."""
    risk = entry - initial_stop
    if price is None or risk <= 0:
        return None
    return (price - entry) / risk


def position_rows(session: Session, now: datetime, price: PriceSource) -> list[render.PositionView]:
    calendar = session_calendar(session)  # once per command, not once per position
    return [
        render.PositionView(
            bot=p.bot,
            symbol=p.symbol,
            qty=p.qty_open,
            entry=p.avg_entry,
            stop=p.current_stop,
            r_so_far=r_so_far(p.avg_entry, p.initial_stop, price(p.instrument_id)),
            days_held=_days_held(calendar, p.opened_at, now),
            virtual=p.is_virtual,
        )
        for p in open_positions(session)
    ]


# --- watchlists --------------------------------------------------------------------------------


def watchlist_texts(session: Session, bots: Sequence[HatBot], day: date) -> list[str]:
    """`/watchlist`: each bot's latest watchlist on or before `day`."""
    found = latest_watchlists(session, day, (b.id for b in bots))
    labels = read_bots.labels(session, (b.id for b in bots))
    out = []
    for bot in bots:
        watchlist = found.get(bot.id)
        out.append(
            render.watchlist_text(
                labels[bot.id],
                watchlist.trade_date if watchlist else None,
                watchlist.entries if watchlist else (),
            )
        )
    return out


def watchlist_digest(session: Session, bot: HatBot, day: date) -> render.WatchlistDigest:
    """HAT-TG-022: new = today minus the previous watchlist, dropped = the previous minus today,
    top = the first min(10, n) of today by score descending."""
    latest = latest_watchlists(session, day, (bot.id,)).get(bot.id)
    today = latest.entries if latest is not None and latest.trade_date == day else ()
    before = latest_watchlists(session, day - timedelta(days=1), (bot.id,)).get(bot.id)
    now_set = {s for s, _ in today}
    previous = {s for s, _ in before.entries} if before is not None else set()
    return render.WatchlistDigest(
        bot=read_bots.label_of(session, bot),
        trade_date=day,
        regime=_regime(session),
        new=tuple(sorted(now_set - previous)),
        dropped=tuple(sorted(previous - now_set)),
        top=tuple(today[:TOP_N]),
    )


# --- reports -----------------------------------------------------------------------------------


def rejection_rows(session: Session, day: date) -> list[render.RejectionRow]:
    """HAT-TG-024, HAT-EXEC-043: every `signal.rejected` event of the trade date, in order."""
    start, end = day_bounds(day)
    events = list(
        session.scalars(
            select(HatEvent)
            .where(
                HatEvent.event_type == "signal.rejected",
                HatEvent.occurred_at >= start,
                HatEvent.occurred_at < end,
            )
            .order_by(HatEvent.id)
        )
    )
    signals = {
        s.id: s
        for s in session.scalars(
            select(HatSignal).where(HatSignal.id.in_({e.aggregate_id for e in events}))
        )
    }
    names = read_bots.labels(session, {s.bot_id for s in signals.values()})
    symbol_names = symbols(session, {s.instrument_id for s in signals.values()})
    rows = []
    for event in events:
        signal = signals.get(event.aggregate_id)
        rows.append(
            render.RejectionRow(
                bot=names.get(signal.bot_id, "?") if signal is not None else "?",
                symbol=symbol_names.get(signal.instrument_id, UNKNOWN)
                if signal is not None
                else "?",
                reason=str((event.payload or {}).get("reason") or "unknown"),
            )
        )
    return rows


def daily_bots(session: Session, day: date) -> list[render.BotDay]:
    """HAT-TG-024: per bot, its latest `hat_daily_bot_snapshot` on or before `day`."""
    bots = bots_named(session, None)
    names = read_bots.labels(session, (bot.id for bot in bots))
    latest = (
        select(
            HatDailyBotSnapshot.bot_id,
            func.max(HatDailyBotSnapshot.trade_date).label("trade_date"),
        )
        .where(HatDailyBotSnapshot.trade_date <= day)
        .group_by(HatDailyBotSnapshot.bot_id)
        .subquery()
    )
    snaps = {
        snap.bot_id: snap
        for snap in session.scalars(
            select(HatDailyBotSnapshot).join(
                latest,
                (HatDailyBotSnapshot.bot_id == latest.c.bot_id)
                & (HatDailyBotSnapshot.trade_date == latest.c.trade_date),
            )
        )
    }
    out = []
    for bot in bots:
        snap = snaps.get(bot.id)
        out.append(
            render.BotDay(
                bot=names[bot.id],
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
