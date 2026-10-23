"""The trade lifecycle as a pure state machine (HAT-EXEC-040..044; D18, Q16).

Eleven states, four of them endings. A trade moves only along `TRANSITIONS`, one row per line of
the spec's table, and its state is the fold of its events over that table: the state stored on
`hat_signal` and `hat_position` is always `fold(events)` (HAT-EXEC-042). Shadow trades use the
same table; only the fills are virtual (HAT-EXEC-044).

Events become steps through `step_of`: most event types are a step of their own, `order.placed`
is an entry or an exit order by its `purpose`, `order.rejected` is a step only when it is final
(the rejection after the single retry, HAT-EXEC-023), `order.cancelled` only for an entry order,
an `order.filled` marked `exit_triggered` stands for the exit order the broker placed when a
resting stop triggered (PR 15c), and the events that carry no transition (other fills,
`signal.approved`, `stop.moved`, `gtt.*` and so on) have no step, nor has an event the journal
kept as a broker fact the table had no row for (`no_step`, PR 36). CONF
`lifecycle_transitions_match_spec` compares this table with the spec's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..errors import HatError

__all__ = [
    "ACTIVE_STATES",
    "ENDINGS",
    "EXIT_TRIGGERED",
    "HOLDING_STATES",
    "INITIAL_STATE",
    "NO_STEP",
    "POSITION_STATES",
    "STATES",
    "STEP_ENTRY_PLACED",
    "STEP_EXIT_PLACED",
    "TRANSITIONS",
    "LifecycleStateError",
    "fold",
    "next_state",
    "step_of",
]

WATCHLISTED = "Watchlisted"
TRIGGERED = "Triggered"
AWAITING_APPROVAL = "AwaitingApproval"
ENTRY_PLACED = "EntryPlaced"
OPEN = "Open"
PARTIALLY_EXITED = "PartiallyExited"
EXIT_PENDING = "ExitPending"
CLOSED = "Closed"
REJECTED = "Rejected"
EXPIRED = "Expired"
CANCELLED = "Cancelled"

ACTIVE_STATES = (
    WATCHLISTED,
    TRIGGERED,
    AWAITING_APPROVAL,
    ENTRY_PLACED,
    OPEN,
    PARTIALLY_EXITED,
    EXIT_PENDING,
)
ENDINGS = (CLOSED, REJECTED, EXPIRED, CANCELLED)
STATES = (*ACTIVE_STATES, *ENDINGS)
INITIAL_STATE = WATCHLISTED
# The states that hold one of a bot's position slots (the per-bot caps count them).
HOLDING_STATES = (ENTRY_PLACED, OPEN, PARTIALLY_EXITED, EXIT_PENDING)
# The states a `hat_position` row takes (HAT-SCH-059).
POSITION_STATES = (OPEN, PARTIALLY_EXITED, EXIT_PENDING, CLOSED)

# The two steps an `order.placed` event drives, told apart by the order's purpose.
STEP_ENTRY_PLACED = "order.placed.entry"
STEP_EXIT_PLACED = "order.placed.exit"

# (from, step) -> to. Steps are named by the event that drives them, with `order.placed` split by
# purpose and `approval.sent` standing for the approval card (it has no event of its own).
ORDER_CANCELLED = "order.cancelled"
TRANSITIONS: dict[tuple[str, str], str] = {
    (WATCHLISTED, "signal.triggered"): TRIGGERED,
    (WATCHLISTED, "watchlist.expired"): EXPIRED,
    (TRIGGERED, "signal.rejected"): REJECTED,
    (TRIGGERED, "approval.sent"): AWAITING_APPROVAL,
    (TRIGGERED, STEP_ENTRY_PLACED): ENTRY_PLACED,
    (AWAITING_APPROVAL, STEP_ENTRY_PLACED): ENTRY_PLACED,
    (AWAITING_APPROVAL, "signal.expired"): EXPIRED,
    (AWAITING_APPROVAL, "signal.skipped"): CANCELLED,
    (AWAITING_APPROVAL, "kill.tripped"): CANCELLED,
    (AWAITING_APPROVAL, "signal.rejected"): REJECTED,
    (ENTRY_PLACED, "position.opened"): OPEN,
    (ENTRY_PLACED, "order.rejected"): REJECTED,
    (ENTRY_PLACED, ORDER_CANCELLED): CANCELLED,
    (OPEN, "exit.partial"): PARTIALLY_EXITED,
    (PARTIALLY_EXITED, "exit.partial"): PARTIALLY_EXITED,
    (OPEN, STEP_EXIT_PLACED): EXIT_PENDING,
    (PARTIALLY_EXITED, STEP_EXIT_PLACED): EXIT_PENDING,
    (EXIT_PENDING, "position.closed"): CLOSED,
}

_EXIT_PURPOSES = frozenset({"exit_partial", "exit_full", "flatten"})
# The payload flag of the `order.filled` that stands for the "(exit order placed)" step when no
# HAT exit order was placed: the first fill of a broker stop (the stop triggered), or a fill
# that empties a position some other way ("full exit rule fires or stop triggers").
EXIT_TRIGGERED = "exit_triggered"
# The payload flag of a broker fact the table had no row for when it was recorded (a retried
# sale's `order.placed` while the trade is already ExitPending): kept in the log, never a step
# (`journal.record`, PR 36).
NO_STEP = "no_step"
# Events that drive a step by their type alone.
_PLAIN_STEPS = frozenset(
    key[1]
    for key in TRANSITIONS
    if key[1] not in {STEP_ENTRY_PLACED, STEP_EXIT_PLACED, ORDER_CANCELLED}
)


class LifecycleStateError(HatError):
    """A step that the transition table does not allow from the current state."""

    def __init__(self, state: str, step: str) -> None:
        super().__init__(f"no transition from {state} on {step}")
        self.state = state
        self.step = step


def step_of(event_type: str, payload: Mapping[str, Any] | None = None) -> str | None:
    """The lifecycle step an event drives, or None when it carries no transition. An
    `order.placed` is an entry order or an exit order by its `purpose`; a partial exit order
    (`exit_partial`) does not move the state, its fill (`exit.partial`) does. An
    `order.rejected` that a retry may still follow carries no step; only a `final` one does. An
    `order.cancelled` is a step for an entry order (or one with no purpose), never for a stop or
    exit order; an `order.filled` is the exit step only when marked `exit_triggered`."""
    data = payload or {}
    if data.get(NO_STEP) is True:
        return None
    if event_type == "order.rejected":
        return event_type if data.get("final") is True else None
    if event_type == ORDER_CANCELLED:
        return event_type if data.get("purpose", "entry") == "entry" else None
    if event_type == "order.filled":
        return STEP_EXIT_PLACED if data.get(EXIT_TRIGGERED) is True else None
    if event_type == "order.placed":
        return _placed_step(data.get("purpose"))
    return event_type if event_type in _PLAIN_STEPS else None


def _placed_step(purpose: object) -> str | None:
    """The step of an `order.placed`: an entry, or an exit order other than a partial one."""
    if purpose == "entry":
        return STEP_ENTRY_PLACED
    if purpose in _EXIT_PURPOSES and purpose != "exit_partial":
        return STEP_EXIT_PLACED
    return None


def next_state(state: str, step: str) -> str:
    """The state after `step`, or `LifecycleStateError` when the table has no such row."""
    try:
        return TRANSITIONS[(state, step)]
    except KeyError:
        raise LifecycleStateError(state, step) from None


def fold(steps: Iterable[str], start: str = INITIAL_STATE) -> str:
    """The state reached by `steps` from `start`; a step the table forbids raises."""
    state = start
    for step in steps:
        state = next_state(state, step)
    return state
