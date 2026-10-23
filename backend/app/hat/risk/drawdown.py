"""Drawdown kills and the nightly bot snapshot (HAT-RISK-060/061/066, HAT-OPS).

`snapshot_bots` writes one `hat_daily_bot_snapshot` per bot and day (equity marked to market,
its restated peak); `live_equity` marks the same equity at a moment of the session;
`evaluate_drawdown_kills` trips the account and bot kills (`kill_ops.trip_kill`) when a point
after the last re-arm is at or past its threshold. The rules are the pure ones in `kill.py`.
Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    HatAllocationDaily,
    HatBot,
    HatDailyBotSnapshot,
    HatFill,
    HatKillSwitch,
    HatOrder,
    HatPosition,
    HatTradeResult,
)
from ..ops.jobs import JobResult
from ..time import IST, as_utc, trade_date_of
from . import kill
from .bots import ladder_state
from .kill_ops import trip_kill
from .policy import RiskPolicy

__all__ = [
    "ALERT_FALLBACK_MARKS",
    "ALERT_KILL_DRAWDOWN",
    "Marks",
    "evaluate_drawdown_kills",
    "live_equity",
    "snapshot_bots",
]

SNAPSHOT_AT = time(15, 30)  # the close: a snapshot is read as taken at the session close (IST)
ALERT_KILL_DRAWDOWN = "kill.drawdown"  # a drawdown kill tripped: the scope is manage-only
ALERT_FALLBACK_MARKS = "drawdown.fallback_marks"  # holdings marked without a fresh price
# An instrument's (latest trade this session, whether it is fresh, previous official close)
Marks = Callable[[uuid.UUID], tuple[Decimal | None, bool, Decimal | None]]


def _snapshot_instant(trade_date: date) -> datetime:
    return datetime.combine(trade_date, SNAPSHOT_AT, tzinfo=IST)


def _last_rearm(session: Session, scope: str, bot_id: uuid.UUID | None) -> datetime | None:
    """When the key's drawdown or manual kill was last re-armed; None for never."""
    return session.scalar(
        select(func.max(HatKillSwitch.rearmed_at)).where(
            HatKillSwitch.scope == scope,
            HatKillSwitch.bot_id.is_(None) if bot_id is None else HatKillSwitch.bot_id == bot_id,
            HatKillSwitch.state == kill.ARMED,
            HatKillSwitch.reason.in_(kill.RISK_REASONS),
        )
    )


def _capital_by_snapshot(
    session: Session, snaps: Sequence[HatDailyBotSnapshot]
) -> dict[tuple[uuid.UUID, date], Decimal]:
    """The allocation capital each snapshot's equity was measured against, so a planned
    reallocation can be told from a loss (HAT-RISK-066): its equity less the realised P&L of the
    real trades closed by it and the marked P&L of those still open (`open_pnl`). Not the
    allocation row of its date: the snapshot runs on its own time (PR 33), so that row may be
    missing or have been written after it. A snapshot with no allocation of its bot on or before
    its date has no known capital (left out)."""
    bots = {s.bot_id for s in snaps}
    first = dict(
        session.execute(
            select(HatAllocationDaily.bot_id, func.min(HatAllocationDaily.trade_date))
            .where(HatAllocationDaily.bot_id.in_(bots))
            .group_by(HatAllocationDaily.bot_id)
        ).all()
    )
    closed: dict[uuid.UUID, list[tuple[datetime, Decimal]]] = {}
    for bot_id, closed_at, pnl in session.execute(
        select(HatPosition.bot_id, HatPosition.closed_at, HatTradeResult.realized_pnl)
        .join(HatTradeResult, HatTradeResult.position_id == HatPosition.id)
        .where(
            HatPosition.bot_id.in_(bots),
            HatPosition.is_virtual.is_(False),
            HatPosition.closed_at.is_not(None),
        )
    ):
        closed.setdefault(bot_id, []).append((as_utc(closed_at), Decimal(pnl)))
    out = {}
    for s in snaps:
        if s.bot_id not in first or first[s.bot_id] > s.trade_date:
            continue
        cut = _snapshot_instant(s.trade_date)
        realized = sum((p for at, p in closed.get(s.bot_id, []) if at <= cut), Decimal(0))
        out[(s.bot_id, s.trade_date)] = Decimal(s.equity) - realized - Decimal(s.open_pnl or 0)
    return out


def evaluate_drawdown_kills(
    session: Session,
    policy: RiskPolicy,
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]] | None = None,
) -> list[str]:
    """Trip the account and bot kills from `hat_daily_bot_snapshot` (HAT-RISK-060/061): a
    switch trips when a snapshot after its last re-arm is at or past its drawdown threshold.
    `live` (each bot's (equity, capital) marked at `now`, `live_equity`) adds a point at `now`
    to each series, so a drawdown trips during the session on the same thresholds (P5, X9).
    Returns the scopes tripped now (`account`, or a bot id).

    Equity is allocation capital plus realised P&L plus the open positions marked to market, so
    a cut in a sleeve's capital by the allocator is not a loss. The bot series carries peaks that
    `snapshot_bots` already restated for reallocation; the account series is restated here from
    the allocation history (`kill.adjusted_series`). A snapshot with zero allocated capital is not
    measured: nothing was at risk (HAT-RISK-066).
    """
    snaps = session.scalars(select(HatDailyBotSnapshot)).all()
    capital = _capital_by_snapshot(session, snaps)
    tripped: list[str] = []
    for bot_id, series in _bot_series(snaps, capital, now, live or {}).items():
        if kill.tripped_since(
            series, policy.bot_drawdown_pct, _last_rearm(session, "bot", bot_id)
        ) and trip_kill(session, scope="bot", bot_id=bot_id, reason="drawdown_bot", now=now):
            tripped.append(str(bot_id))
    account = _account_series(snaps, capital, now, live or {})
    if kill.tripped_since(
        account, policy.account_drawdown_pct, _last_rearm(session, "account", None)
    ) and trip_kill(session, scope="account", bot_id=None, reason="drawdown_account", now=now):
        tripped.append("account")
    return tripped


def _bot_series(
    snaps: Sequence[HatDailyBotSnapshot],
    capital: Mapping[tuple[uuid.UUID, date], Decimal],
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
) -> dict[uuid.UUID, list[kill.EquityPoint]]:
    """Each bot's snapshot points with their stored peaks, then its point marked at `now`."""
    by_bot: dict[uuid.UUID, list[kill.EquityPoint]] = {}
    last: dict[uuid.UUID, kill.EquityPoint] = {}
    last_capital: dict[uuid.UUID, Decimal | None] = {}
    for s in sorted(snaps, key=lambda row: row.trade_date):
        point = kill.EquityPoint(
            _snapshot_instant(s.trade_date), Decimal(s.equity), Decimal(s.equity_peak)
        )
        last[s.bot_id] = point
        last_capital[s.bot_id] = capital.get((s.bot_id, s.trade_date))
        if last_capital[s.bot_id] != 0:
            by_bot.setdefault(s.bot_id, []).append(point)
    for bot_id, (equity, cap) in live.items():
        if cap != 0:
            point = kill.live_point(last.get(bot_id), last_capital.get(bot_id), now, equity, cap)
            by_bot.setdefault(bot_id, []).append(point)
    return by_bot


def _account_series(
    snaps: Sequence[HatDailyBotSnapshot],
    capital: Mapping[tuple[uuid.UUID, date], Decimal],
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
) -> list[kill.EquityPoint]:
    """The account's points: the bots' snapshots summed per date with peaks restated for
    reallocation (`kill.adjusted_series`), then the sum of the bots marked at `now`."""
    totals: dict[date, tuple[Decimal, Decimal | None]] = {}
    for s in snaps:
        equity, cap = totals.get(s.trade_date, (Decimal(0), Decimal(0)))
        known = capital.get((s.bot_id, s.trade_date))
        totals[s.trade_date] = (
            equity + Decimal(s.equity),
            None if cap is None or known is None else cap + known,
        )
    account = kill.adjusted_series(
        kill.SnapshotPoint(_snapshot_instant(day), equity, cap)
        for day, (equity, cap) in totals.items()
    )
    live_capital = sum((cap for _, cap in live.values()), Decimal(0))
    if live_capital != 0:
        previous = account[-1] if account else None
        known = None if previous is None else totals[previous.at.date()][1]
        equity = sum((equity for equity, _ in live.values()), Decimal(0))
        account.append(kill.live_point(previous, known, now, equity, live_capital))
    return account


def _realized(session: Session, bot_id: uuid.UUID, cut: datetime) -> Decimal:
    """The bot's cumulative realised P&L of real trades closed by `cut`."""
    return sum(
        (
            Decimal(r.realized_pnl)
            for r in session.scalars(
                select(HatTradeResult)
                .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                .where(
                    HatPosition.bot_id == bot_id,
                    HatPosition.is_virtual.is_(False),
                    HatPosition.closed_at <= cut,
                )
            )
        ),
        Decimal(0),
    )


def _holdings(session: Session, bot_id: uuid.UUID, marks: Marks) -> list[kill.Holding]:
    """The bot's open real positions as `kill.open_pnl` marks them: the net cash of each one's
    fills (`write_result` sums the same fills once it closes), its stop, its latest trade and
    its previous close."""
    out = []
    for position in session.scalars(
        select(HatPosition).where(
            HatPosition.bot_id == bot_id,
            HatPosition.is_virtual.is_(False),
            HatPosition.closed_at.is_(None),
        )
    ):
        cash = Decimal(0)
        for side, qty, price, fee in session.execute(
            select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == position.signal_id)
        ):
            cash += (1 if side == "sell" else -1) * qty * Decimal(price) - Decimal(fee)
        price, fresh, close = marks(position.instrument_id)
        out.append(
            kill.Holding(
                position.qty_open, cash, Decimal(position.current_stop), price, fresh, close
            )
        )
    return out


def _allocation_on(session: Session, bot_id: uuid.UUID, day: date) -> HatAllocationDaily | None:
    """The bot's latest allocation on or before `day` (the capital in force)."""
    return session.scalar(
        select(HatAllocationDaily)
        .where(HatAllocationDaily.bot_id == bot_id, HatAllocationDaily.trade_date <= day)
        .order_by(HatAllocationDaily.trade_date.desc())
        .limit(1)
    )


def live_equity(
    session: Session, now: datetime, marks: Marks
) -> tuple[dict[uuid.UUID, tuple[Decimal, Decimal]], dict[str, int]]:
    """Each allocated bot's equity marked at `now` (capital in force + realised P&L so far +
    its open positions at `marks`, `kill.open_pnl`) with its capital, for
    `evaluate_drawdown_kills(live=...)`; and how many holdings were marked without a fresh
    trade, by basis (`kill.mark_of`: the previous close, or the lower of the trade and the
    stop)."""
    out: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    fallbacks = {kill.MARK_CLOSE: 0, kill.MARK_STOP: 0}
    for bot in session.scalars(select(HatBot)).all():
        allocation = _allocation_on(session, bot.id, trade_date_of(now))
        if allocation is None:
            continue
        pnl, missed = kill.open_pnl(_holdings(session, bot.id, marks))
        for basis, count in missed.items():
            fallbacks[basis] += count
        capital = Decimal(allocation.capital)
        out[bot.id] = (capital + _realized(session, bot.id, now) + pnl, capital)
    return out, fallbacks


def _snapshot_peak(
    session: Session, bot_id: uuid.UUID, trade_date: date, equity: Decimal, capital: Decimal
) -> Decimal:
    """The running peak at `trade_date`: the previous snapshot's peak restated for any change of
    allocation capital since (`kill.rebase_peak`), or, when the bot has no earlier snapshot, its
    capital (a new sleeve starts there, so a first day's loss is measured; `kill.live_point`).
    Rounded up to the stored 4 places, so rounding never hides a drawdown.

    The previous capital is the one that snapshot used (its equity less its P&L), not
    an allocation row read now: the snapshot runs on its own time (PR 33), so an `allocate` that
    committed after it, or a night with no allocation of its own, would give another capital."""
    previous = session.scalars(
        select(HatDailyBotSnapshot)
        .where(HatDailyBotSnapshot.bot_id == bot_id, HatDailyBotSnapshot.trade_date < trade_date)
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()
    if previous is None:
        return max(equity, capital)
    before = _capital_by_snapshot(session, [previous]).get((bot_id, previous.trade_date))
    base = kill.rebase_peak(
        Decimal(previous.equity), Decimal(previous.equity_peak), before, capital
    )
    return max(equity, base.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def _no_marks(_instrument_id: uuid.UUID) -> tuple[Decimal | None, bool, Decimal | None]:
    return None, False, None


def snapshot_bots(
    session: Session, trade_date: date, marks: Marks = _no_marks
) -> tuple[JobResult, int]:
    """`snapshot.bots` (HAT-OPS): write one `hat_daily_bot_snapshot` per bot for `trade_date`.
    Returns the result and how many holdings were marked by a fallback.

    Equity = the sleeve's allocation capital + its cumulative realised P&L + its open real
    positions marked at `marks` (the day's official closes; P5, X9: `open_pnl`, stored); the
    peak is the largest equity seen so far, restated for any change of allocation capital so a
    planned reallocation never reads as a loss (HAT-RISK-066, `_snapshot_peak`); open risk is the
    sum of (entry - stop) x qty over its open real positions. Idempotent: a rerun for the
    same date rewrites the same row, so a crash after the body and before the success is
    recorded does no harm. The capital is the bot's latest allocation on or before the date
    (PR 33): the job runs on its own time, not after `allocate`, so a night whose quality gate
    failed (no allocation for the date) still measures the drawdown kills on the capital in
    force. Bots never allocated are skipped.
    """
    rows = 0
    fallbacks = 0
    for bot in session.scalars(select(HatBot)).all():
        allocation = _allocation_on(session, bot.id, trade_date)
        if allocation is None:
            continue
        pnl, missed = kill.open_pnl(_holdings(session, bot.id, marks))
        fallbacks += sum(missed.values())
        realized = _realized(session, bot.id, _snapshot_instant(trade_date))
        equity = Decimal(allocation.capital) + realized + pnl
        peak = _snapshot_peak(session, bot.id, trade_date, equity, Decimal(allocation.capital))
        open_risk = sum(
            (
                max(Decimal(p.avg_entry) - Decimal(p.current_stop), Decimal(0)) * p.qty_open
                for p in session.scalars(
                    select(HatPosition).where(
                        HatPosition.bot_id == bot.id,
                        HatPosition.is_virtual.is_(False),
                        HatPosition.closed_at.is_(None),
                    )
                )
            ),
            Decimal(0),
        )
        day_start = _snapshot_instant(trade_date).replace(hour=0, minute=0)
        day_r = sum(
            (
                Decimal(r.r_multiple)
                for r in session.scalars(
                    select(HatTradeResult)
                    .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                    .where(
                        HatPosition.bot_id == bot.id,
                        HatPosition.is_virtual.is_(False),
                        HatPosition.closed_at >= day_start,
                        HatPosition.closed_at <= _snapshot_instant(trade_date),
                    )
                )
            ),
            Decimal(0),
        )
        values = {
            "equity": equity,
            "equity_peak": peak,
            "open_pnl": pnl,
            "open_risk": open_risk,
            "realized_r": day_r,
            "exposure_level": ladder_state(session, bot.id).level,
            "currency": allocation.currency,
        }
        existing = session.get(HatDailyBotSnapshot, (bot.id, trade_date))
        if existing is None:
            session.add(HatDailyBotSnapshot(bot_id=bot.id, trade_date=trade_date, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        rows += 1
    return JobResult(calls=0, rows=rows), fallbacks
