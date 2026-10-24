"""Drawdown kills and the nightly bot snapshot (HAT-RISK-060/061/066, HAT-OPS).

`snapshot_bots` writes one `hat_daily_bot_snapshot` per bot and day (equity marked to market,
its restated peak); `live_equity` marks the same equity at a moment of the session;
`evaluate_drawdown_kills` trips the account and bot kills (`kill_ops.trip_kill`) when a point
after the last re-arm is at or past its threshold. A re-arm restarts the peak (D232): a re-armed
key is measured from its equity at the first point after the re-arm (`hat_drawdown_base`) and
reads nothing older. The rules are the pure ones in `kill.py`. Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..events import record
from ..models import (
    HatAllocationDaily,
    HatBot,
    HatDailyBotSnapshot,
    HatDrawdownBase,
    HatFill,
    HatKillSwitch,
    HatOrder,
    HatPosition,
    HatTradeResult,
)
from ..ops.jobs import JobResult
from ..settings import load_settings
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


def _last_rearms(session: Session) -> dict[tuple[str, uuid.UUID | None], HatKillSwitch]:
    """Each key's latest re-arm row of a drawdown or manual kill, by (scope, bot), in one query
    (Postgres `DISTINCT ON`); a key never re-armed is missing."""
    rows = session.scalars(
        select(HatKillSwitch)
        .where(HatKillSwitch.state == kill.ARMED, HatKillSwitch.reason.in_(kill.RISK_REASONS))
        .order_by(HatKillSwitch.scope, HatKillSwitch.bot_id, HatKillSwitch.rearmed_at.desc())
        .distinct(HatKillSwitch.scope, HatKillSwitch.bot_id)
    )
    return {(row.scope, row.bot_id): row for row in rows}


def _point(s: HatDailyBotSnapshot) -> kill.SnapshotPoint:
    """The snapshot as a point with the allocation it was measured against (Q1: stored)."""
    return kill.SnapshotPoint(
        _snapshot_instant(s.trade_date),
        Decimal(s.equity),
        None if s.capital is None else Decimal(s.capital),
        None if s.final_weight is None else Decimal(s.final_weight),
    )


def _summed(points: Sequence[kill.SnapshotPoint], at: datetime) -> kill.SnapshotPoint:
    """The bots' points summed into the account's at `at`: an unknown capital or weight of any
    bot leaves the sum's unknown. All of a date's allocations share one funds total, so the
    summed weight restates the summed capital (`kill.restated_capital`)."""
    capital: Decimal | None = Decimal(0)
    weight: Decimal | None = Decimal(0)
    for p in points:
        capital = None if capital is None or p.capital is None else capital + p.capital
        weight = None if weight is None or p.weight is None else weight + p.weight
    return kill.SnapshotPoint(at, sum((p.equity for p in points), Decimal(0)), capital, weight)


def evaluate_drawdown_kills(
    session: Session,
    policy: RiskPolicy,
    now: datetime,
    live: Mapping[uuid.UUID, kill.SnapshotPoint] | None = None,
) -> list[str]:
    """Trip the account and bot kills from `hat_daily_bot_snapshot` (HAT-RISK-060/061): a
    switch trips when a point after its last re-arm is at or past its drawdown threshold.
    `live` (each bot's equity marked at `now` with its allocation, `live_equity`) adds a point at
    `now` to each series, so a drawdown trips during the session on the same thresholds (P5, X9).
    Returns the scopes tripped now (`account`, or a bot id).

    Equity is allocation capital (weight x the broker funds total, which carries the realised
    P&L, Q1) plus the realised P&L not yet in it plus the open positions marked to market. A
    change of weight by the allocator is not a loss; a change of the funds total is a result
    (`kill.restated_capital`). A key never re-armed reads its whole history: the bot series
    carries peaks that `snapshot_bots` already restated; the account series is restated here
    (`kill.adjusted_series`). A re-armed key reads only from its re-arm, and its peak restarts
    at its equity there (D232, `_tripped_after_rearm`). A point with zero allocated capital is
    not measured: nothing was at risk (HAT-RISK-066).
    """
    live = live or {}
    history = _History(session, now, live)
    keys: list[tuple[str, uuid.UUID | None, Decimal]] = [
        ("bot", bot_id, policy.bot_drawdown_pct)
        for bot_id in sorted(set(session.scalars(select(HatBot.id))) | set(live))
    ]
    keys.append(("account", None, policy.account_drawdown_pct))
    rearms = _last_rearms(session)
    tripped: list[str] = []
    for scope, bot_id, threshold in keys:
        rearm = rearms.get((scope, bot_id))
        if rearm is None:
            breached = kill.tripped_since(history.series(bot_id), threshold)
        else:
            breached = _tripped_after_rearm(session, rearm, threshold, now, _live_of(live, bot_id))
        reason = "drawdown_bot" if scope == "bot" else "drawdown_account"
        if breached and trip_kill(session, scope=scope, bot_id=bot_id, reason=reason, now=now):
            tripped.append("account" if bot_id is None else str(bot_id))
    return tripped


class _History:
    """The whole snapshot history, read once and only for a key never re-armed."""

    def __init__(
        self, session: Session, now: datetime, live: Mapping[uuid.UUID, kill.SnapshotPoint]
    ) -> None:
        self._session = session
        self._now = now
        self._live = live
        self._read: (
            tuple[dict[uuid.UUID, list[kill.EquityPoint]], list[kill.EquityPoint]] | None
        ) = None

    def series(self, bot_id: uuid.UUID | None) -> list[kill.EquityPoint]:
        """The bot's series, or the account's for None."""
        if self._read is None:
            snaps = self._session.scalars(select(HatDailyBotSnapshot)).all()
            self._read = (_bot_series(snaps, self._live), _account_series(snaps, self._live))
        bots, account = self._read
        return account if bot_id is None else bots.get(bot_id, [])


def _live_of(
    live: Mapping[uuid.UUID, kill.SnapshotPoint], bot_id: uuid.UUID | None
) -> kill.SnapshotPoint | None:
    """The scope's point marked now: the bot's, or the bots' summed for the account; None when
    there is none or its capital is zero (nothing at risk)."""
    if bot_id is not None:
        found = live.get(bot_id)
    elif live:
        points = list(live.values())
        found = _summed(points, points[0].at)
    else:
        found = None
    return found if found is not None and found.capital != 0 else None


def _points_since(
    session: Session,
    bot_id: uuid.UUID | None,
    since: datetime,
    now: datetime,
    live: kill.SnapshotPoint | None,
) -> list[kill.SnapshotPoint]:
    """The scope's points after `since`: its snapshots (the bots' summed per date for the
    account), then its point marked at `now`."""
    query = select(HatDailyBotSnapshot).where(
        HatDailyBotSnapshot.trade_date >= since.astimezone(IST).date()
    )
    if bot_id is not None:
        query = query.where(HatDailyBotSnapshot.bot_id == bot_id)
    by_date: dict[date, list[kill.SnapshotPoint]] = {}
    for s in session.scalars(query):
        if _snapshot_instant(s.trade_date) > since:
            by_date.setdefault(s.trade_date, []).append(_point(s))
    out = [_summed(by_date[day], _snapshot_instant(day)) for day in sorted(by_date)]
    if live is not None and now > since:
        out.append(live)
    return out


def _tripped_after_rearm(
    session: Session,
    rearm: HatKillSwitch,
    threshold: Decimal,
    now: datetime,
    live: kill.SnapshotPoint | None,
) -> bool:
    """D232 (Q41): whether the re-armed key has fallen `threshold` from its equity at the
    re-arm. The first point after the re-arm becomes its base (`hat_drawdown_base`, journalled
    as `kill.peak_reset`); until there is one nothing is measured. Reads from the base on."""
    base = session.get(HatDrawdownBase, rearm.id)
    since = as_utc(rearm.rearmed_at if base is None else base.taken_at)
    points = _points_since(session, rearm.bot_id, since, now, live)
    if base is None:
        if not points:
            return False
        base = _take_base(session, rearm, points[0])
    start = kill.SnapshotPoint(
        as_utc(base.taken_at),
        Decimal(base.equity),
        Decimal(base.capital),
        None if base.weight is None else Decimal(base.weight),
    )
    return kill.tripped_after_rearm(start, points, threshold)


def _take_base(
    session: Session, rearm: HatKillSwitch, point: kill.SnapshotPoint
) -> HatDrawdownBase:
    """Record `point` as the re-armed key's base and journal `kill.peak_reset`."""
    allocations = select(HatAllocationDaily.currency).order_by(HatAllocationDaily.trade_date.desc())
    if rearm.bot_id is not None:
        allocations = allocations.where(HatAllocationDaily.bot_id == rearm.bot_id)
    assert point.capital is not None  # a point after a re-arm always knows its capital (Q1)
    base = HatDrawdownBase(
        rearm_id=rearm.id,
        taken_at=point.at,
        equity=point.equity,
        capital=point.capital,
        weight=point.weight,
        # a point exists only for an allocated bot (or account of them)
        currency=session.scalar(allocations.limit(1)),
    )
    session.add(base)
    session.flush()
    record(
        session,
        aggregate_type="bot",
        aggregate_id=rearm.bot_id if rearm.bot_id is not None else load_settings().user_id,
        event_type="kill.peak_reset",
        actor="engine",
        payload={
            "scope": rearm.scope,
            "rearmed_at": as_utc(rearm.rearmed_at).isoformat(),
            "equity": str(point.equity),
        },
        bot_id=rearm.bot_id,
        occurred_at=point.at,
    )
    return base


def _bot_series(
    snaps: Sequence[HatDailyBotSnapshot], live: Mapping[uuid.UUID, kill.SnapshotPoint]
) -> dict[uuid.UUID, list[kill.EquityPoint]]:
    """Each bot's snapshot points with their stored peaks, then its point marked now."""
    by_bot: dict[uuid.UUID, list[kill.EquityPoint]] = {}
    last: dict[uuid.UUID, tuple[kill.SnapshotPoint, Decimal]] = {}
    for s in sorted(snaps, key=lambda row: row.trade_date):
        point = _point(s)
        last[s.bot_id] = (point, Decimal(s.equity_peak))
        if point.capital != 0:
            by_bot.setdefault(s.bot_id, []).append(
                kill.EquityPoint(point.at, point.equity, Decimal(s.equity_peak))
            )
    for bot_id, now_point in live.items():
        if now_point.capital != 0:
            previous, peak = last.get(bot_id, (None, Decimal(0)))
            by_bot.setdefault(bot_id, []).append(kill.live_point(previous, peak, now_point))
    return by_bot


def _account_series(
    snaps: Sequence[HatDailyBotSnapshot], live: Mapping[uuid.UUID, kill.SnapshotPoint]
) -> list[kill.EquityPoint]:
    """The account's points: the bots' snapshots summed per date with peaks restated for
    reallocation (`kill.adjusted_series`), then the sum of the bots marked now."""
    by_date: dict[date, list[kill.SnapshotPoint]] = {}
    for s in snaps:
        by_date.setdefault(s.trade_date, []).append(_point(s))
    totals = {day: _summed(points, _snapshot_instant(day)) for day, points in by_date.items()}
    account = kill.adjusted_series(totals.values())
    now_point = _live_of(live, None)
    if now_point is not None:
        previous = account[-1] if account else None
        known = None if previous is None else totals[previous.at.astimezone(IST).date()]
        peak = Decimal(0) if previous is None else previous.peak
        account.append(kill.live_point(known, peak, now_point))
    return account


def _unfunded(session: Session, bot_id: uuid.UUID, funded: date, cut: datetime) -> Decimal:
    """The realised P&L of the bot's real trades closed by `cut` and after the session of
    `funded`, the trade date of the allocation in force: the broker funds total that allocation
    read carries the trades closed by then, so only the later ones are added (Q1)."""
    after = datetime.combine(funded + timedelta(days=1), time(0), tzinfo=IST)
    return sum(
        (
            Decimal(pnl)
            for pnl in session.scalars(
                select(HatTradeResult.realized_pnl)
                .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                .where(
                    HatPosition.bot_id == bot_id,
                    HatPosition.is_virtual.is_(False),
                    HatPosition.closed_at >= after,
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
) -> tuple[dict[uuid.UUID, kill.SnapshotPoint], dict[str, int]]:
    """Each allocated bot's equity marked at `now` (capital in force + the realised P&L not yet
    in it, `_unfunded` + its open positions at `marks`, `kill.open_pnl`) with its capital and
    weight, for `evaluate_drawdown_kills(live=...)`; and how many holdings were marked without a
    fresh trade, by basis (`kill.mark_of`: the previous close, or the lower of the trade and the
    stop)."""
    out: dict[uuid.UUID, kill.SnapshotPoint] = {}
    fallbacks = {kill.MARK_CLOSE: 0, kill.MARK_STOP: 0}
    for bot in session.scalars(select(HatBot)).all():
        allocation = _allocation_on(session, bot.id, trade_date_of(now))
        if allocation is None:
            continue
        pnl, missed = kill.open_pnl(_holdings(session, bot.id, marks))
        for basis, count in missed.items():
            fallbacks[basis] += count
        capital = Decimal(allocation.capital)
        realized = _unfunded(session, bot.id, allocation.trade_date, now)
        out[bot.id] = kill.SnapshotPoint(
            now, capital + realized + pnl, capital, Decimal(allocation.final_weight)
        )
    return out, fallbacks


def _rearmed_between(session: Session, bot_id: uuid.UUID, after: datetime, by: datetime) -> bool:
    """Whether the bot's drawdown or manual kill was re-armed after `after` and by `by`."""
    found = session.scalar(
        select(func.count())
        .select_from(HatKillSwitch)
        .where(
            HatKillSwitch.scope == "bot",
            HatKillSwitch.bot_id == bot_id,
            HatKillSwitch.state == kill.ARMED,
            HatKillSwitch.reason.in_(kill.RISK_REASONS),
            HatKillSwitch.rearmed_at > after,
            HatKillSwitch.rearmed_at <= by,
        )
    )
    return bool(found)


def _snapshot_peak(session: Session, bot_id: uuid.UUID, point: kill.SnapshotPoint) -> Decimal:
    """The running peak at `point`: the previous snapshot's peak restated for any change of
    weight since (`kill.live_point`), or, when the bot has no earlier snapshot, its capital (a
    new sleeve starts there, so a first day's loss is measured). A re-arm of the bot's drawdown
    or manual kill since the previous snapshot restarts the peak at the equity (QA10), so the
    health multiplier, which reads this peak, restarts with the kill (D232). Rounded up to the
    stored 4 places, so rounding never hides a drawdown.

    The previous allocation is the one that snapshot stored, not an allocation row read now:
    the snapshot runs on its own time (PR 33), so an `allocate` that committed after it, or a
    night with no allocation of its own, would give another."""
    previous = session.scalars(
        select(HatDailyBotSnapshot)
        .where(
            HatDailyBotSnapshot.bot_id == bot_id,
            HatDailyBotSnapshot.trade_date < point.at.astimezone(IST).date(),
        )
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()
    if previous is None:
        peak = kill.live_point(None, Decimal(0), point).peak
    elif _rearmed_between(session, bot_id, _snapshot_instant(previous.trade_date), point.at):
        peak = point.equity
    else:
        peak = kill.live_point(_point(previous), Decimal(previous.equity_peak), point).peak
    return peak.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


def _no_marks(_instrument_id: uuid.UUID) -> tuple[Decimal | None, bool, Decimal | None]:
    return None, False, None


def snapshot_bots(
    session: Session, trade_date: date, marks: Marks = _no_marks
) -> tuple[JobResult, int]:
    """`snapshot.bots` (HAT-OPS): write one `hat_daily_bot_snapshot` per bot for `trade_date`.
    Returns the result and how many holdings were marked by a fallback.

    Equity = the sleeve's allocation capital + the realised P&L not yet in it (Q1,
    `_unfunded`) + its open real positions marked at `marks` (the day's official closes; P5,
    X9: `open_pnl`, stored), stored with the allocation's capital and weight; the peak is the
    largest equity seen so far, restated for any change of weight so a planned reallocation
    never reads as a loss (HAT-RISK-066, `_snapshot_peak`); open risk is the
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
        cut = _snapshot_instant(trade_date)
        capital = Decimal(allocation.capital)
        point = kill.SnapshotPoint(
            cut,
            capital + _unfunded(session, bot.id, allocation.trade_date, cut) + pnl,
            capital,
            Decimal(allocation.final_weight),
        )
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
        day_start = cut.replace(hour=0, minute=0)
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
                        HatPosition.closed_at <= cut,
                    )
                )
            ),
            Decimal(0),
        )
        values = {
            "equity": point.equity,
            "equity_peak": _snapshot_peak(session, bot.id, point),
            "capital": capital,
            "final_weight": Decimal(allocation.final_weight),
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
