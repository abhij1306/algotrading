"""The trade lifecycle as a pure state machine (07 §4, HAT-EXEC-040..044; D18, Q16).

Eleven states, four of them endings. A trade moves only along `TRANSITIONS`, one row per line of
the spec's table, and its state is the fold of its events over that table: the state stored on
`hat_signal` and `hat_position` is always `fold(events)` (HAT-EXEC-042). Shadow trades use the
same table; only the fills are virtual (HAT-EXEC-044).

Events become steps through `step_of`: most event types are a step of their own, `order.placed`
is an entry or an exit order by its `purpose`, and the events that carry no transition
(`order.filled`, `signal.approved`, `stop.moved`, `gtt.*` and so on) have no step. CONF
`lifecycle_transitions_match_spec` compares this table with the spec's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..errors import HatError

__all__ = [
    "ACTIVE_STATES",
    "ENDINGS",
    "HOLDING_STATES",
    "INITIAL_STATE",
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
# The states that hold one of a bot's position slots (the per-bot caps count them, 06 §3).
HOLDING_STATES = (ENTRY_PLACED, OPEN, PARTIALLY_EXITED, EXIT_PENDING)

# The two steps an `order.placed` event drives, told apart by the order's purpose.
STEP_ENTRY_PLACED = "order.placed.entry"
STEP_EXIT_PLACED = "order.placed.exit"

# (from, step) -> to. Steps are named by the event that drives them, with `order.placed` split by
# purpose and `approval.sent` standing for the approval card (it has no event of its own).
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
    (ENTRY_PLACED, "order.cancelled"): CANCELLED,
    (OPEN, "exit.partial"): PARTIALLY_EXITED,
    (PARTIALLY_EXITED, "exit.partial"): PARTIALLY_EXITED,
    (OPEN, STEP_EXIT_PLACED): EXIT_PENDING,
    (PARTIALLY_EXITED, STEP_EXIT_PLACED): EXIT_PENDING,
    (EXIT_PENDING, "position.closed"): CLOSED,
}

_EXIT_PURPOSES = frozenset({"exit_partial", "exit_full", "flatten"})
# Events that drive a step by their type alone.
_PLAIN_STEPS = frozenset(
    key[1] for key in TRANSITIONS if key[1] not in {STEP_ENTRY_PLACED, STEP_EXIT_PLACED}
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
    (`exit_partial`) does not move the state, its fill (`exit.partial`) does."""
    if event_type == "order.placed":
        purpose = (payload or {}).get("purpose")
        if purpose == "entry":
            return STEP_ENTRY_PLACED
        if purpose in _EXIT_PURPOSES and purpose != "exit_partial":
            return STEP_EXIT_PLACED
        return None
    return event_type if event_type in _PLAIN_STEPS else None


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
