"""Rules for the runtime tables that the app derives rather than the database enforces.

Virtual and real rows share tables (HAT-SCH-017): shadow mode writes `hat_order`, `hat_fill` and
`hat_position` rows with `is_virtual = true`. The flag is derived here, once, so every writer
(the router in PR 15, the lifecycle in PR 11) gets it from the same function.
"""

from __future__ import annotations

from .risk.modes import Mode

__all__ = ["child_is_virtual", "position_is_virtual"]


def position_is_virtual(mode: str, *, skipped: bool = False) -> bool:
    """Whether a position is virtual: its signal's mode was shadow, or the signal was skipped in
    confirm mode (a skipped trade continues as a shadow trade for the veto score).

    A skipped auto-mode signal does not exist (auto never asks), so `skipped` only matters in
    confirm mode.
    """
    if mode not in Mode:
        raise ValueError("unknown signal mode")
    return mode == Mode.SHADOW or (mode == Mode.CONFIRM and skipped)


def child_is_virtual(parent_is_virtual: bool) -> bool:
    """An order inherits its position's flag and a fill inherits its order's: a real order is
    never filled virtually and a virtual order never reaches a broker."""
    return parent_is_virtual
