"""The governance vocabulary the models validate with: the execution modes and the risk-policy
cooling-off rule. Pure and a leaf (it imports only `errors` and `time`), so `app.hat.models` and
the data layer that imports it pull no risk code. `Mode` is the one spelling of the modes: the
schema CHECKs, the strategy config and the router all read it. Mode changes (the queue) live in
`risk.modes`; the policy record in `risk.policy`."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from .errors import HatError
from .time import as_utc

__all__ = [
    "COOLING_OFF",
    "MODE_VALUES",
    "CoolingOffError",
    "Mode",
    "ModeField",
    "cooling_off_satisfied",
    "require_cooling_off",
]


class Mode(StrEnum):
    """How a bot's entries are routed: shadow (virtual, never at the broker), confirm (an
    approval card first) or auto (placed; only a high-confidence signal, D149)."""

    SHADOW = "shadow"
    CONFIRM = "confirm"
    AUTO = "auto"


# Membership of any value, a plain `str` included: `x in Mode` raises TypeError for a str before
# Python 3.12 (the project supports 3.11); a set of the members does not.
MODE_VALUES: frozenset[str] = frozenset(Mode)

# A config field holding a mode: validates and dumps as the plain string (pydantic `Literal`).
ModeField = Literal[Mode.SHADOW, Mode.CONFIRM, Mode.AUTO]


# Risk increases take effect no sooner than this after they are requested (assumption A in
# HAT-SCH-084). The Postgres CHECK states the same span in hours (168), so a server time zone
# with daylight saving cannot change it.
COOLING_OFF = timedelta(days=7)


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
