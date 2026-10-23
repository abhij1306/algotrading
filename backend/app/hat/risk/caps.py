"""Caps and invariants as pure predicates (HAT-RISK-030..036, 050, 070..076).

Each predicate answers one question about a new entry and holds no state. A cap can only reject
an entry; it never applies to an exit (the exit path in `decision.py` does not call any of
them). The invariants here live in code, outside every config, so no edit can remove them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .exposure import max_positions
from .policy import RiskPolicy
from .sessions import SessionCalendar

# The documented max-chase default per strategy (HAT-CRIT-122) and the fallback for any other
# strategy (OWNER-PENDING: the stricter value). Percentages, not fractions.
MAX_CHASE_DEFAULT_PCT = Decimal(2)
MAX_CHASE_BY_STRATEGY = {
    "stage2-contraction-breakout": Decimal(5),
    "momentum-breakout-ep": Decimal(2),
}


@dataclass(frozen=True)
class OpenPosition:
    """A position that is open now, as the caps read it."""

    bot_id: object
    instrument_id: object
    industry: str
    qty: int
    avg_entry: Decimal
    last_price: Decimal
    current_stop: Decimal
    is_virtual: bool = False

    @property
    def open_risk(self) -> Decimal:
        """(entry - stop) x qty; zero once the stop has been trailed to or above entry, so a
        profit-locked position never offsets the risk of another."""
        return max(self.avg_entry - self.current_stop, Decimal(0)) * self.qty


def global_open_risk_ok(
    open_risks: Iterable[Decimal], candidate_risk: Decimal, total_equity: Decimal, pct: Decimal
) -> bool:
    """HAT-RISK-030: the open risk of every bot plus the candidate's stays within `pct` of the
    account's total equity."""
    return sum(open_risks, Decimal(0)) + candidate_risk <= pct * total_equity


def positions_ok(open_count: int, level: str, policy: RiskPolicy) -> bool:
    """HAT-RISK-031: fewer open positions than min(policy maximum, the level's maximum)."""
    return open_count < max_positions(level, policy)


def industry_ok(open_in_industry: int, policy: RiskPolicy) -> bool:
    """HAT-RISK-032."""
    return open_in_industry < policy.max_positions_per_industry


def entries_ok(entries_today: int, policy: RiskPolicy) -> bool:
    """HAT-RISK-033."""
    return entries_today < policy.max_entries_per_day


def stopouts_ok(stopouts_today: int, policy: RiskPolicy) -> bool:
    """HAT-RISK-034."""
    return stopouts_today < policy.max_stopouts_per_day


def sessions_since(calendar: SessionCalendar, stop_day: date, day: date) -> int:
    """The calendar's trading sessions after `stop_day` up to and including `day`: 0 on the stop's
    own session."""
    return sum(1 for d, row in calendar.days.items() if row.is_trading and stop_day < d <= day)


def cooldown_ok(
    calendar: SessionCalendar, last_hard_stop: date | None, day: date, policy: RiskPolicy
) -> bool:
    """HAT-RISK-036: with `cooldown_sessions` N > 0, an instrument the bot last closed by
    `hard_stop` on `last_hard_stop` is refused on that session and the N sessions after it.
    N = 0, or no hard stop at or before `day`, refuses nothing."""
    n = policy.cooldown_sessions
    if n == 0 or last_hard_stop is None or last_hard_stop > day:
        return True
    return sessions_since(calendar, last_hard_stop, day) > n


def regime_gate_open(gate_on: bool) -> bool:
    """HAT-RISK-050. There is no override: nothing else is read."""
    return gate_on is True


def stop_valid(entry: Decimal, stop: Decimal) -> bool:
    """HAT-RISK-073 and HAT-RISK-022: a stop strictly between 0 and the entry price."""
    return 0 < stop < entry


def effective_max_chase_pct(configured: Decimal | None, strategy_key: str) -> Decimal:
    """HAT-RISK-076: the playbook's last configured percentage, else the strategy's documented
    default, else 2. A deleted or disabled criterion leaves `configured` as its last value or
    None; either way a percentage is returned."""
    if configured is not None:
        return configured
    return MAX_CHASE_BY_STRATEGY.get(strategy_key, MAX_CHASE_DEFAULT_PCT)


def chase_ok(price: Decimal, trigger_level: Decimal, pct: Decimal, *, locked_upper: bool) -> bool:
    """HAT-RISK-076: the price is at most `pct` percent above the trigger level, and the stock
    is not locked at its upper circuit."""
    return not locked_upper and price * 100 <= trigger_level * (100 + pct)


def is_loser(position: OpenPosition) -> bool:
    """HAT-RISK-071: the last price is below the average entry."""
    return position.last_price < position.avg_entry
