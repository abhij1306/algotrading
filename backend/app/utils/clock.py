"""Clock seam: the implementation lives in `app.hat.clock` (K1); this module re-exports it so the
legacy code keeps one `Clock` type with HAT. Delete with the legacy code (K4/K5)."""

from __future__ import annotations

from app.hat.clock import SYSTEM_CLOCK, Clock, FixedClock, SystemClock, require_aware

__all__ = ["SYSTEM_CLOCK", "Clock", "FixedClock", "SystemClock", "require_aware"]
