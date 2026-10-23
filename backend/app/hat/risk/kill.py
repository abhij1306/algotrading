"""Kill switches and manage-only, as pure rules (HAT-RISK-060..065, HAT-SCH-033).

Manage-only means no new entries; stops and exits keep running. A bot is manage-only when the
account kill is tripped, its own kill is tripped, its health kill is tripped or it is paused.
Drawdown and manual kills re-arm only by hand with a second confirmation; a health kill clears
by itself once the checks are healthy. The model is `verification/tla/KillSwitch.tla`.

A kill key is (scope, bot, channel). The channel separates what can clear by itself (`health`)
from what cannot (`risk`: drawdown and manual). Within the health channel each reason is a stream
of its own (the quality gate's `data_health`, the live feed's `feed_health`, `broker_health`): a
check trips and clears only its own reason, so the feed recovering never clears a failed gate
(HAT-DATA-065). The latest row of each stream is its state, so an automatic health re-arm can
never hide a tripped drawdown kill or another source's health trip.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from ..time import as_utc
from .sizing import MissingInputError, exact_input

TRIPPED = "tripped"
ARMED = "armed"
HEALTH_REASONS = ("data_health", "feed_health", "broker_health")
RISK_REASONS = ("drawdown_account", "drawdown_bot", "manual")
REASONS = (*RISK_REASONS, *HEALTH_REASONS)


def channel_of(reason: str) -> str:
    """`health` for a reason that clears by itself, else `risk`."""
    return "health" if reason in HEALTH_REASONS else "risk"


def stream_of(reason: str) -> str:
    """The stream a row belongs to: each health reason alone, the risk reasons together (a
    confirmed re-arm ends a drawdown or manual trip alike)."""
    return reason if reason in HEALTH_REASONS else "risk"


@dataclass(frozen=True)
class KillRow:
    """One `hat_kill_switch` row. A tripped row has `tripped_at`; a re-arm row is `armed` and
    carries `rearmed_at` (and the `tripped_at` of the trip it ends)."""

    scope: str
    bot_id: UUID | None
    state: str
    tripped_at: datetime
    reason: str
    rearmed_at: datetime | None = None

    @property
    def occurred_at(self) -> datetime:
        """When this row's state began: the re-arm time for an armed row, else the trip time."""
        if self.state == ARMED and self.rearmed_at is not None:
            return as_utc(self.rearmed_at)
        return as_utc(self.tripped_at)


def _latest(rows: Iterable[KillRow]) -> KillRow | None:
    """The row with the greatest event time; on a tie a tripped row wins (fail safe), then the
    later one in the input order."""
    best: KillRow | None = None
    for row in rows:
        if best is None:
            best = row
            continue
        key = (row.occurred_at, row.state == TRIPPED)
        best_key = (best.occurred_at, best.state == TRIPPED)
        if key >= best_key:
            best = row
    return best


def current_state(
    rows: Iterable[KillRow],
    scope: str,
    bot_id: UUID | None,
    channel: str | None = None,
    *,
    stream: str | None = None,
) -> str:
    """The state of (scope, bot) from its rows: `tripped` when any stream of the key is
    tripped (each stream is read from its latest row), else `armed`. With `channel` set, only
    that channel's streams are read; with `stream` set, only that stream. A key with no rows is
    armed."""
    mine = [
        r
        for r in rows
        if r.scope == scope
        and r.bot_id == bot_id
        and (channel is None or channel_of(r.reason) == channel)
        and (stream is None or stream_of(r.reason) == stream)
    ]
    for name in {stream_of(r.reason) for r in mine}:
        latest = _latest(r for r in mine if stream_of(r.reason) == name)
        if latest is not None and latest.state == TRIPPED:
            return TRIPPED
    return ARMED


@dataclass(frozen=True)
class KillState:
    """The switches that bear on one bot, as the entry gate reads them."""

    account_tripped: bool = False
    bot_tripped: bool = False
    health_tripped: bool = False
    paused: bool = False

    @property
    def manage_only(self) -> bool:
        return self.account_tripped or self.bot_tripped or self.health_tripped or self.paused


def kill_state_of(rows: Sequence[KillRow], bot_id: UUID, *, paused: bool) -> KillState:
    """The `KillState` of one bot: the account key, the bot's key and its health channel."""
    return KillState(
        account_tripped=current_state(rows, "account", None, "risk") == TRIPPED,
        bot_tripped=current_state(rows, "bot", bot_id, "risk") == TRIPPED,
        health_tripped=(
            current_state(rows, "bot", bot_id, "health") == TRIPPED
            or current_state(rows, "account", None, "health") == TRIPPED
        ),
        paused=paused,
    )


# --- Drawdown (HAT-RISK-060/061, HAT-DEC-B26) -------------------------------------------------


@dataclass(frozen=True)
class EquityPoint:
    """One snapshot of equity against its running peak."""

    at: datetime
    equity: Decimal
    peak: Decimal


def drawdown_breached(equity: Decimal, peak: Decimal, threshold: Decimal) -> bool:
    """True when (peak - equity) / peak >= threshold, by exact cross-multiplication.
    A non-positive or non-finite peak, or a negative or non-finite equity, is a breach: an
    unreadable equity fails closed instead of reading as no drawdown."""
    try:
        eq = exact_input(equity, "equity")
        top = exact_input(peak, "peak")
        limit = exact_input(threshold, "threshold")
    except MissingInputError:
        return True
    if top <= 0 or eq < 0:
        return True
    return top - eq >= limit * top


def tripped_since(
    series: Iterable[EquityPoint], threshold: Decimal, since: datetime | None = None
) -> bool:
    """True when some snapshot taken after `since` (the last re-arm; None for never) breached the
    drawdown threshold. Snapshots at or before the re-arm do not count."""
    for point in series:
        if since is not None and as_utc(point.at) <= as_utc(since):
            continue
        if drawdown_breached(point.equity, point.peak, threshold):
            return True
    return False


def rebase_peak(
    prev_equity: Decimal,
    prev_peak: Decimal,
    prev_capital: Decimal | None,
    capital: Decimal | None,
) -> Decimal:
    """The previous peak restated at a new allocation, so a planned reallocation is not a loss.

    Snapshot equity is allocation capital plus realised P&L, so cutting a sleeve's
    capital (a regime or health change, HAT-PORT-050) lowers equity without any trade losing
    money. The previous equity moves to `prev_equity + (capital - prev_capital)` and the peak
    scales by the same ratio, which keeps (peak - equity) / peak exactly as it was before the
    reallocation: only trading results move the drawdown (HAT-RISK-066). Unknown capital on
    either side (no allocation row) or no change leaves the peak as it is; a base that is not
    positive after the move (nothing left to measure against) restarts the peak at zero, so the
    next equity becomes the peak.
    """
    try:
        equity = exact_input(prev_equity, "equity")
        peak = exact_input(prev_peak, "peak")
        old = None if prev_capital is None else exact_input(prev_capital, "capital")
        new = None if capital is None else exact_input(capital, "capital")
    except MissingInputError:
        return prev_peak
    if old is None or new is None or old == new:
        return peak
    moved = equity + (new - old)
    if equity <= 0 or moved <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 60  # wide enough that the ratio of two stored amounts is exact
        return peak * moved / equity


@dataclass(frozen=True)
class SnapshotPoint:
    """One time's equity and the allocation capital it was measured against (None: unknown)."""

    at: datetime
    equity: Decimal
    capital: Decimal | None = None


def adjusted_series(points: Iterable[SnapshotPoint]) -> list[EquityPoint]:
    """Equity points with peaks restated for planned reallocation (`rebase_peak`).

    A point whose allocation capital is zero has nothing at risk (a sleeve out of Live, or an
    account with every sleeve benched): it moves the running peak but is not itself returned, so
    it can never read as a breach. With no capital known anywhere this is the plain running
    maximum of the equity.
    """
    out: list[EquityPoint] = []
    previous: tuple[Decimal, Decimal, Decimal | None] | None = None  # equity, peak, capital
    for point in sorted(points, key=lambda p: as_utc(p.at)):
        if previous is None:
            peak = point.equity
        else:
            base = rebase_peak(previous[0], previous[1], previous[2], point.capital)
            peak = max(point.equity, base)
        previous = (point.equity, peak, point.capital)
        if point.capital is None or point.capital != 0:
            out.append(EquityPoint(point.at, point.equity, peak))
    return out


# --- Mark-to-market equity (P5, owner decision X9) ---------------------------------------------


@dataclass(frozen=True)
class Holding:
    """One open real position as its equity is marked: the shares still held, the net cash of
    its fills so far (sales less purchases, less fees: negative while held), its broker stop,
    the latest trade of its instrument this session (None: none yet) with whether that trade is
    fresh, and its previous official close (the last stored daily close; None: none known)."""

    qty: int
    cash: Decimal
    stop: Decimal
    price: Decimal | None
    fresh: bool
    close: Decimal | None = None


MARK_TRADE = "trade"  # a fresh trade
MARK_CLOSE = "close"  # no trade yet this session: the previous official close (D210)
MARK_STOP = "stop"  # a stale trade, or no trade and no close: the lower of the trade and the stop


def _usable(value: Decimal | None) -> Decimal | None:
    """`value` as a positive finite price, else None."""
    if value is None:
        return None
    try:
        found = exact_input(value, "price")
    except MissingInputError:
        return None
    return found if found > 0 else None


def mark_of(holding: Holding) -> tuple[Decimal, str]:
    """The price `holding` is marked at and its basis. A fresh trade is the mark. Before the
    instrument's first trade of the session, its previous official close (D210: the stop would
    read a gap that has not traded as a loss). Otherwise (a trade gone stale, an unreadable one,
    no trade and no close) the most conservative price known: the lower of the last trade and
    the stop, so a stale price never marks a holding higher than either; zero when neither is
    known (the shares count for nothing)."""
    price = _usable(holding.price)
    if price is not None and holding.fresh:
        return price, MARK_TRADE
    close = _usable(holding.close)
    if holding.price is None and close is not None:
        return close, MARK_CLOSE
    known = [p for p in (price, _usable(holding.stop)) if p is not None]
    return (min(known) if known else Decimal(0)), MARK_STOP


def open_pnl(holdings: Iterable[Holding]) -> tuple[Decimal, dict[str, int]]:
    """The marked P&L of the open holdings (each one's fill cash plus its shares at its mark,
    which is its realised P&L once it is sold out) and how many were marked on each basis other
    than a fresh trade (`MARK_CLOSE`, `MARK_STOP`)."""
    total = Decimal(0)
    bases = {MARK_CLOSE: 0, MARK_STOP: 0}
    for holding in holdings:
        price, basis = mark_of(holding)
        total += holding.cash + holding.qty * price
        if basis != MARK_TRADE:
            bases[basis] += 1
    return total, bases


def live_point(
    previous: EquityPoint | None,
    previous_capital: Decimal | None,
    at: datetime,
    equity: Decimal,
    capital: Decimal,
) -> EquityPoint:
    """The equity marked at `at` against its running peak: the last snapshot's peak restated
    for any change of capital since (`rebase_peak`), or the capital itself before the first
    snapshot (a new sleeve starts at its capital), and never below the equity."""
    if previous is None:
        return EquityPoint(at, equity, max(equity, capital))
    base = rebase_peak(previous.equity, previous.peak, previous_capital, capital)
    return EquityPoint(at, equity, max(equity, base))
