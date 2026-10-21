"""Caps and invariants as pure predicates (06 §3, §5, §7; HAT-RISK-030..035, 050, 070..076).

Each predicate answers one question about a new entry and holds no state. A cap can only reject
an entry; it never applies to an exit (the exit path in `decision.py` does not call any of
them). The invariants here live in code, outside every config, so no edit can remove them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .exposure import max_positions
from .policy import RiskPolicy
from .sessions import SessionCalendar, SessionState

# The documented max-chase default per strategy (HAT-CRIT-122) and the fallback for any other
# strategy (OWNER-PENDING: the stricter value). Percentages, not fractions.
MAX_CHASE_DEFAULT_PCT = Decimal(2)
MAX_CHASE_BY_STRATEGY = {
    "stage2-contraction-breakout": Decimal(5),
    "momentum-breakout-ep": Decimal(2),
}
FEED_STALE_AFTER_SECONDS = 30  # HAT-DATA-116


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


def regime_gate_open(gate_on: bool) -> bool:
    """HAT-RISK-050. There is no override: nothing else is read."""
    return gate_on is True


def stop_valid(entry: Decimal, stop: Decimal) -> bool:
    """HAT-RISK-073 and HAT-RISK-022: a stop strictly between 0 and the entry price."""
    return 0 < stop < entry


def session_open(calendar: SessionCalendar, at: datetime) -> SessionState:
    """HAT-RISK-075: the session state at `at` from the calendar (never from DEV_MODE)."""
    return calendar.state(at)


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
