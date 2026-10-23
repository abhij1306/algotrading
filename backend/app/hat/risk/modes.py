"""The execution modes, and mode changes: queued outside market hours, effective at the next
session open (HAT-GOV-084, HAT-SCH-086, HAT-TG-032). Pure; the queue's rows are
`hat_mode_change`. `Mode` is the one spelling of the modes: the schema CHECKs, the strategy
config and the router all read it."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from ..errors import HatError
from ..time import as_utc
from .sessions import SessionCalendar, SessionState


class Mode(StrEnum):
    """How a bot's entries are routed: shadow (virtual, never at the broker), confirm (an
    approval card first) or auto (placed; only a high-confidence signal, D149)."""

    SHADOW = "shadow"
    CONFIRM = "confirm"
    AUTO = "auto"
# Membership of any value, a plain `str` included: `x in Mode` raises TypeError for a str

# Membership of any value, a plain `str` included: `x in MODE_VALUES` raises TypeError for a str
# before Python 3.12 (the project supports 3.11), a set of the members does not.
MODE_VALUES: frozenset[str] = frozenset(Mode)

# A config field holding a mode: validates and dumps as the plain string (pydantic `Literal`).
ModeField = Literal[Mode.SHADOW, Mode.CONFIRM, Mode.AUTO]


class ModeChangeError(HatError):
    """A mode change was refused (inside market hours, an unknown mode, or no known next open)."""


@dataclass(frozen=True)
class ModeChange:
    to_mode: str
    requested_at: datetime
    effective_from: datetime


def plan_mode_change(to_mode: str, requested_at: datetime, calendar: SessionCalendar) -> ModeChange:
    """The queued change: refused unless `to_mode` is shadow, confirm or auto and the calendar
    says the market is closed at `requested_at`; effective at the first session open after it.
    An unknown calendar date, or no later open on record, is a refusal (fail closed)."""
    if to_mode not in MODE_VALUES:
        raise ModeChangeError("a mode is shadow, confirm or auto")
    if calendar.state(requested_at) is not SessionState.CLOSED:
        raise ModeChangeError("a mode changes only outside market hours")
    effective = calendar.next_open_after(requested_at)
    if effective is None:
        raise ModeChangeError("the calendar has no later session open")
    return ModeChange(to_mode, as_utc(requested_at), effective)


def queued_mode_at(initial: str, changes: Iterable[ModeChange], at: datetime) -> str:
    """The mode in force at `at`: the latest queued change whose `effective_from` has passed
    (ties broken by the later request), else `initial`."""
    moment = as_utc(at)
    due = [c for c in changes if as_utc(c.effective_from) <= moment]
    if not due:
        return initial
    return max(due, key=lambda c: (as_utc(c.effective_from), as_utc(c.requested_at))).to_mode
