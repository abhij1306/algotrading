"""Runtime tables (03-schema §5): watchlist, signals, approvals, orders, fills, positions and
trade results.

Every signal records the exact strategy and risk-policy version that produced it, so any trade
can be replayed and explained. `hat_position.current_stop` is guarded by the Postgres trigger
`hat_position_stop_guard` (invariant 1, `app/hat/schema.py`).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)

from ...base import Base
from ._types import Currency, JsonB, Money, Ratio, Timestamptz, fk, in_values, uuid_pk

__all__ = [
    "APPROVAL_DECISIONS",
    "POSITION_STATES",
    "TRADE_STATES",
    "ORDER_PURPOSES",
    "ORDER_SIDES",
    "ORDER_STATUSES",
    "SIGNAL_MODES",
    "HatApproval",
    "HatFill",
    "HatOrder",
    "HatPosition",
    "HatSignal",
    "HatSignalAnnouncement",
    "HatTradeResult",
    "HatWatchlistEntry",
]

BOT_ID = "hat_bot.id"
INSTRUMENT_ID = "hat_instrument.id"
SIGNAL_ID = "hat_signal.id"

SIGNAL_MODES = ("shadow", "confirm", "auto")
ORDER_PURPOSES = ("entry", "stop_day", "stop_gtt", "exit_partial", "exit_full", "flatten")
ORDER_SIDES = ("buy", "sell")
APPROVAL_DECISIONS = ("take", "skip", "expired")
# The 11 trade-lifecycle states of 07 §4 (D18, Q16) in table order; a position holds the last four
# active ones and Closed. `app.hat.engine.lifecycle` owns the transitions (CONF
# `lifecycle_transitions_match_spec` compares them).
TRADE_STATES = (
    "Watchlisted",
    "Triggered",
    "AwaitingApproval",
    "EntryPlaced",
    "Open",
    "PartiallyExited",
    "ExitPending",
    "Closed",
    "Rejected",
    "Expired",
    "Cancelled",
)
POSITION_STATES = ("Open", "PartiallyExited", "ExitPending", "Closed")
# Owner-pending default (HAT-SCH-058): derived from the event catalogue and SUBMIT_UNCONFIRMED.
ORDER_STATUSES = (
    "pending",
    "placed",
    "unconfirmed",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
)


class HatWatchlistEntry(Base):
    __tablename__ = "hat_watchlist_entry"
    __table_args__ = (Index("hat_watchlist_entry_bot_date", "bot_id", "trade_date"),)

    id = uuid_pk()
    bot_id = fk(BOT_ID)
    strategy_version_id = fk("hat_strategy_version.id")
    # `default` for single-playbook bots.
    playbook = Column(Text, nullable=False)
    instrument_id = fk(INSTRUMENT_ID)
    trade_date = Column(Date, nullable=False)
    score = Column(Ratio, nullable=False)
    criteria_results = Column(JsonB, nullable=False)
    pivot = Column(Money)
    planned_stop = Column(Money)
    adtr = Column(Money)
    status = Column(Text, nullable=False)


class HatSignal(Base):
    """One signal per (bot, playbook, instrument, day); its `id` is the idempotency key."""

    __tablename__ = "hat_signal"
    __table_args__ = (
        UniqueConstraint("bot_id", "playbook", "instrument_id", "trade_date"),
        CheckConstraint("qty >= 0", name="ck_hat_signal_qty"),
        CheckConstraint(in_values("mode", SIGNAL_MODES), name="ck_hat_signal_mode"),
        CheckConstraint(in_values("state", TRADE_STATES), name="hat_signal_state_check"),
        Index("hat_signal_bot_date", "bot_id", "trade_date"),
    )

    id = uuid_pk()
    bot_id = fk(BOT_ID)
    strategy_version_id = fk("hat_strategy_version.id")
    risk_policy_version_id = fk("hat_risk_policy_version.id")
    playbook = Column(Text, nullable=False)
    instrument_id = fk(INSTRUMENT_ID)
    trade_date = Column(Date, nullable=False)
    triggered_at = Column(Timestamptz, nullable=False)
    trigger_price = Column(Money, nullable=False)
    stop_price = Column(Money, nullable=False)
    qty = Column(Integer, nullable=False)
    risk_amount = Column(Money, nullable=False)
    currency = Column(Currency, nullable=False)
    # The mode in force when the signal triggered.
    mode = Column(Text, nullable=False)
    # Trade lifecycle state (07 §4); `app.hat.engine.lifecycle` moves it with its event.
    state = Column(Text, nullable=False)
    risk_decision = Column(JsonB, nullable=False)


class HatSignalAnnouncement(Base):
    """EP catalysts attached to a signal."""

    __tablename__ = "hat_signal_announcement"

    signal_id = fk(SIGNAL_ID, primary_key=True)
    announcement_id = fk("hat_announcement.id", primary_key=True)


class HatApproval(Base):
    """Skipped signals continue as shadow trades for the veto score."""

    __tablename__ = "hat_approval"
    __table_args__ = (
        CheckConstraint(
            "decision IS NULL OR " + in_values("decision", APPROVAL_DECISIONS),
            name="ck_hat_approval_decision",
        ),
    )

    signal_id = fk(SIGNAL_ID, primary_key=True)
    sent_at = Column(Timestamptz, nullable=False)
    expires_at = Column(Timestamptz, nullable=False)
    decided_at = Column(Timestamptz)
    decision = Column(Text)
    price_at_decision = Column(Money)
    telegram_message_id = Column(BigInteger)
    # A hash of the signed token; the token itself is never stored (HAT-SCH-056).
    token_hash = Column(Text, nullable=False)


class HatOrder(Base):
    __tablename__ = "hat_order"
    __table_args__ = (
        UniqueConstraint("tag"),
        CheckConstraint(in_values("purpose", ORDER_PURPOSES), name="ck_hat_order_purpose"),
        CheckConstraint(in_values("side", ORDER_SIDES), name="ck_hat_order_side"),
        CheckConstraint(in_values("status", ORDER_STATUSES), name="ck_hat_order_status"),
        CheckConstraint("qty > 0", name="ck_hat_order_qty"),
        Index("hat_order_broker_order_id", "broker_order_id"),
    )

    id = uuid_pk()
    signal_id = fk(SIGNAL_ID)
    position_id = fk("hat_position.id", nullable=True)
    broker_account_id = fk("hat_broker_account.id")
    purpose = Column(Text, nullable=False)
    side = Column(Text, nullable=False)
    qty = Column(Integer, nullable=False)
    order_type = Column(Text, nullable=False)
    price = Column(Money)
    trigger_price = Column(Money)
    # 20 characters: `HT` + purpose code + signal encoding (PLAN C12; 07 §10).
    tag = Column(Text, nullable=False)
    # Null until the broker confirms; always null when virtual.
    broker_order_id = Column(Text)
    status = Column(Text, nullable=False)
    is_virtual = Column(Boolean, nullable=False)
    placed_at = Column(Timestamptz)
    currency = Column(Currency, nullable=False)


class HatFill(Base):
    __tablename__ = "hat_fill"
    __table_args__ = (
        UniqueConstraint("order_id", "broker_trade_id"),
        CheckConstraint("qty > 0", name="ck_hat_fill_qty"),
        # A real fill needs its broker trade id, or NULLs would defeat the unique key.
        CheckConstraint(
            "is_virtual OR broker_trade_id IS NOT NULL", name="ck_hat_fill_broker_trade_id"
        ),
    )

    id = uuid_pk()
    order_id = fk("hat_order.id")
    qty = Column(Integer, nullable=False)
    price = Column(Money, nullable=False)
    fees = Column(Money, nullable=False)
    currency = Column(Currency, nullable=False)
    filled_at = Column(Timestamptz, nullable=False)
    # Null for virtual fills.
    broker_trade_id = Column(Text)
    is_virtual = Column(Boolean, nullable=False)


class HatPosition(Base):
    """The trigger `hat_position_stop_guard` blocks any `current_stop` move away from price."""

    __tablename__ = "hat_position"
    __table_args__ = (
        CheckConstraint("qty_open >= 0", name="ck_hat_position_qty_open"),
        CheckConstraint(in_values("state", POSITION_STATES), name="hat_position_state_check"),
        # The update trigger cannot see an INSERT: a position starts with its initial stop.
        CheckConstraint("current_stop >= initial_stop", name="ck_hat_position_stop_tightens"),
        Index(
            "hat_position_open_by_bot",
            "bot_id",
            postgresql_where=text("closed_at IS NULL"),
            sqlite_where=text("closed_at IS NULL"),
        ),
    )

    id = uuid_pk()
    bot_id = fk(BOT_ID)
    signal_id = fk(SIGNAL_ID)
    instrument_id = fk(INSTRUMENT_ID)
    is_virtual = Column(Boolean, nullable=False)
    qty_open = Column(Integer, nullable=False)
    avg_entry = Column(Money, nullable=False)
    initial_stop = Column(Money, nullable=False)
    current_stop = Column(Money, nullable=False)
    currency = Column(Currency, nullable=False)
    opened_at = Column(Timestamptz, nullable=False)
    closed_at = Column(Timestamptz)
    state = Column(Text, nullable=False)
    # Exit rules already fired (fire-once).
    rules_fired = Column(JsonB, nullable=False)


class HatTradeResult(Base):
    """Written once, when the position closes (the primary key allows one row per position)."""

    __tablename__ = "hat_trade_result"

    position_id = fk("hat_position.id", primary_key=True)
    realized_pnl = Column(Money, nullable=False)
    fees = Column(Money, nullable=False)
    currency = Column(Currency, nullable=False)
    r_multiple = Column(Ratio, nullable=False)
    holding_days = Column(Integer, nullable=False)
    exit_reason = Column(Text, nullable=False)
    mfe_r = Column(Ratio, nullable=False)
    mae_r = Column(Ratio, nullable=False)
