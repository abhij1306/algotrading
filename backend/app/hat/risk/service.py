"""`evaluate_signal`: the single risk authority for a HAT signal (HAT-RISK-100).

It works out the two outcomes the decision composes, whether the market is open at the signal's
instant and whether the entry price lies within the price band around the last traded price, then
calls the pure `decide` and returns ACCEPT with a quantity or REJECT with a typed reason. It never
raises: a missing or unreadable input is a REJECT. An exit or a protective-stop move is always
accepted.

Moved here from the legacy `RiskManager.evaluate_signal` (K1, backend review C5), so HAT's risk
authority depends on nothing outside `app.hat`. Two things that method did are gone:
- the DEV_MODE bypass: HAT never has one (`MarketHoursPolicy` has no dev switch);
- the read of the legacy VCP halt flag: HAT's own kill switches (`KillState`) are the only
  halt (HAT-RISK-104).

`decide` is the only caller of `size_position` (CONF `risk.single_sizing_path`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext

from ..data.sessions import IST, MarketHoursPolicy
from ..time import trade_date_of
from .decision import EXIT_ACTIONS, EntryChecks, RiskDecision, SignalContext, decide
from .sessions import SessionCalendar

# A limit entry further than this percent from the last traded price is out of band. It is the
# value the legacy risk manager used (`max_price_deviation_pct`); it is a constant, not a policy
# field, because the policy document's fields are fixed by 06 §1 (HAT-RISK-010).
MAX_PRICE_DEVIATION_PCT = Decimal(20)
_PRECISION = 120  # wide enough that no product of a plausible input is rounded


def market_open_at(at: object, hours: MarketHoursPolicy) -> bool | None:
    """Whether the market is open at `at` under `hours`; None when `at` is not an aware instant."""
    if isinstance(at, datetime) and at.tzinfo is not None and at.utcoffset() is not None:
        return hours.is_open(at)
    return None


def _policy_for(ctx: SignalContext) -> MarketHoursPolicy:
    """The default policy: the weekday session, plus the calendar's session of the signal's day."""
    sessions = {}
    calendar, at = ctx.calendar, ctx.at
    if isinstance(calendar, SessionCalendar) and isinstance(at, datetime) and at.tzinfo:
        day = calendar.days.get(trade_date_of(at))
        if day is not None and day.is_trading and day.open_at and day.close_at:
            sessions[trade_date_of(at)] = (
                day.open_at.astimezone(IST).time(),
                day.close_at.astimezone(IST).time(),
            )
    return MarketHoursPolicy(sessions=sessions)


def price_band_ok(
    entry: object, ltp: object, band_pct: Decimal = MAX_PRICE_DEVIATION_PCT
) -> bool | None:
    """Whether `entry` is within `band_pct` percent of `ltp`: |entry - ltp| * 100 <= band * ltp.

    None when it cannot be evaluated: either price is not an exact finite Decimal, or there is no
    live price (`ltp` not above zero), so the decision fails closed."""
    if not (isinstance(entry, Decimal) and isinstance(ltp, Decimal)):
        return None
    if not (isinstance(band_pct, Decimal) and band_pct.is_finite() and band_pct >= 0):
        return None
    if not (entry.is_finite() and ltp.is_finite()) or ltp <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return abs(entry - ltp) * 100 <= band_pct * ltp


def evaluate_signal(
    ctx: SignalContext,
    hours: MarketHoursPolicy | None = None,
    band_pct: Decimal = MAX_PRICE_DEVIATION_PCT,
) -> RiskDecision:
    """ACCEPT with a quantity or REJECT with a typed reason for one signal (HAT-RISK-100).

    `hours` is the market-hours policy to apply. By default it is the NSE weekday session plus the
    calendar's own session for the signal's trade date, so a special session (Muhurat, a Saturday
    session) is open when `ctx.calendar` says so; the decision requires the calendar to say OPEN
    as well, so a holiday or a closed day stays closed either way.
    """
    if ctx.action in EXIT_ACTIONS:
        return decide(ctx, EntryChecks(market_open=None, price_band_ok=None))
    return decide(
        ctx,
        EntryChecks(
            market_open=market_open_at(ctx.at, hours or _policy_for(ctx)),
            price_band_ok=price_band_ok(ctx.entry_price, ctx.ltp, band_pct),
        ),
    )
