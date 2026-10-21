"""Risk-policy governance rules that are pure functions (06 §1, 03-schema §8).

Imports nothing from the database, broker or models, so the rule is the same in the app, in
the backtest and in the property tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..errors import HatError
from ..time import as_utc

# Risk increases take effect no sooner than this after they are requested (assumption A in
# HAT-SCH-084). The Postgres CHECK states the same span in hours (168), so a server time zone
# with daylight saving cannot change it.
COOLING_OFF = timedelta(days=7)
DIRECTIONS = ("initial", "increase", "decrease")


class CoolingOffError(HatError):
    """A risk increase was scheduled before the cooling-off period ended."""


def cooling_off_satisfied(direction: str, requested_at: datetime, effective_from: datetime) -> bool:
    """True when a risk-policy version of `direction` may take effect at `effective_from`.

    Only an `increase` waits: `initial` and `decrease` are never delayed.
    """
    if direction != "increase":
        return True
    return as_utc(effective_from) - as_utc(requested_at) >= COOLING_OFF


def require_cooling_off(direction: str, requested_at: datetime, effective_from: datetime) -> None:
    """Raise `CoolingOffError` unless `cooling_off_satisfied`."""
    if not cooling_off_satisfied(direction, requested_at, effective_from):
        raise CoolingOffError("a risk increase must take effect at least 7 days after request")
