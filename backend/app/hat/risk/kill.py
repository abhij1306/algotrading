"""Kill switches and manage-only, as pure rules (06 §6, HAT-RISK-060..065, HAT-SCH-033).

Manage-only means no new entries; stops and exits keep running. A bot is manage-only when the
account kill is tripped, its own kill is tripped, its health kill is tripped or it is paused.
Drawdown and manual kills re-arm only by hand with a second confirmation; a health kill clears
by itself once the checks are healthy. The model is `verification/tla/KillSwitch.tla`.

A kill key is (scope, bot, channel). The channel separates what can clear by itself (`health`)
from what cannot (`risk`: drawdown and manual). The latest row of a key is its state, so an
automatic health re-arm can never hide a tripped drawdown kill (a single latest-row-per-scope
reading would let it).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from ..time import as_utc
from .sizing import MissingInputError, exact

TRIPPED = "tripped"
ARMED = "armed"
HEALTH_REASONS = ("data_health", "broker_health")
RISK_REASONS = ("drawdown_account", "drawdown_bot", "manual")
REASONS = (*RISK_REASONS, *HEALTH_REASONS)


def channel_of(reason: str) -> str:
    """`health` for a reason that clears by itself, else `risk`."""
    return "health" if reason in HEALTH_REASONS else "risk"


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
    rows: Iterable[KillRow], scope: str, bot_id: UUID | None, channel: str | None = None
) -> str:
    """The state of (scope, bot) from its rows: `tripped` when any channel of the key is
    tripped (each channel is read from its latest row), else `armed`. With `channel` set, only
    that channel is read. A key with no rows is armed."""
    mine = [r for r in rows if r.scope == scope and r.bot_id == bot_id]
    channels = {channel} if channel else {channel_of(r.reason) for r in mine}
    for name in channels:
        latest = _latest(r for r in mine if channel_of(r.reason) == name)
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
        eq = exact(equity, "equity")
        top = exact(peak, "peak")
        limit = exact(threshold, "threshold")
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

    Snapshot equity is allocation capital plus realised P&L (06 §2), so cutting a sleeve's
    capital (a regime or health change, HAT-PORT-050) lowers equity without any trade losing
    money. The previous equity moves to `prev_equity + (capital - prev_capital)` and the peak
    scales by the same ratio, which keeps (peak - equity) / peak exactly as it was before the
    reallocation: only trading results move the drawdown (HAT-RISK-066). Unknown capital on
    either side (no allocation row) or no change leaves the peak as it is; a base that is not
    positive after the move (nothing left to measure against) restarts the peak at zero, so the
    next equity becomes the peak.
    """
    try:
        equity = exact(prev_equity, "equity")
        peak = exact(prev_peak, "peak")
        old = None if prev_capital is None else exact(prev_capital, "capital")
        new = None if capital is None else exact(capital, "capital")
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


def account_series(bot_snapshots: Iterable[tuple[datetime, Decimal]]) -> list[EquityPoint]:
    """Account equity points from per-bot snapshots `(time, equity)`: the equity of each time is
    the sum over bots, and the peak is the running maximum of those sums."""
    totals: dict[datetime, Decimal] = {}
    for at, equity in bot_snapshots:
        key = as_utc(at)
        totals[key] = totals.get(key, Decimal(0)) + equity
    points = []
    peak: Decimal | None = None
    for at in sorted(totals):
        peak = totals[at] if peak is None else max(peak, totals[at])
        points.append(EquityPoint(at, totals[at], peak))
    return points
