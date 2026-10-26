"""Risk-control state in hat_state: the queued mode changes, the kill switches and each bot's
exposure level. `bot_id` names a bot config record (`app.hat.configs`): a plain column."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Integer, Text

from ...base import StateBase
from ..governance import Mode
from ._types import Timestamptz, in_values, now_utc, ref, uuid_pk

__all__ = ["HatExposureState", "HatKillSwitch", "HatModeChange"]


class HatModeChange(StateBase):
    __tablename__ = "hat_mode_change"
    __table_args__ = (
        CheckConstraint(in_values("to_mode", Mode), name="ck_hat_mode_change_to_mode"),
    )

    id = uuid_pk()
    bot_id = ref()
    from_mode = Column(Text, nullable=False)
    to_mode = Column(Text, nullable=False)
    requested_at = Column(Timestamptz, nullable=False, default=now_utc)
    # The next session open, from hat_trading_calendar (set by the app).
    effective_from = Column(Timestamptz, nullable=False)
    via = Column(Text, nullable=False)
    # What the session open did with it (`mode_changes.apply_mode_changes`): applied, or
    # refused with the gate that failed. Both stay null until its open.
    applied_at = Column(Timestamptz)
    refused_reason = Column(Text)


class HatKillSwitch(StateBase):
    __tablename__ = "hat_kill_switch"
    __table_args__ = (
        CheckConstraint(in_values("scope", ("account", "bot")), name="ck_hat_kill_switch_scope"),
        CheckConstraint(in_values("state", ("tripped", "armed")), name="ck_hat_kill_switch_state"),
        CheckConstraint(
            "(scope = 'bot' AND bot_id IS NOT NULL) OR (scope = 'account' AND bot_id IS NULL)",
            name="ck_hat_kill_switch_scope_bot",
        ),
    )

    id = uuid_pk()
    scope = Column(Text, nullable=False)
    bot_id = ref(nullable=True)
    state = Column(Text, nullable=False)
    tripped_at = Column(Timestamptz, nullable=False)
    reason = Column(Text, nullable=False)
    rearmed_at = Column(Timestamptz)


class HatExposureState(StateBase):
    __tablename__ = "hat_exposure_state"
    __table_args__ = (
        CheckConstraint(in_values("level", ("L1", "L2", "L3")), name="ck_hat_exposure_state_level"),
        CheckConstraint("consecutive_wins >= 0", name="ck_hat_exposure_state_wins"),
        CheckConstraint("consecutive_losses >= 0", name="ck_hat_exposure_state_losses"),
    )

    bot_id = ref(primary_key=True)
    level = Column(Text, nullable=False)
    consecutive_wins = Column(Integer, nullable=False)
    consecutive_losses = Column(Integer, nullable=False)
    updated_at = Column(Timestamptz, nullable=False, default=now_utc)
