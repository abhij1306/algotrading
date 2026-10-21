"""PBT runtime.virtual_consistency (HAT-SCH-017): virtual and real rows share tables, and the
`is_virtual` flag of a position, its orders and their fills agree with the signal that opened it.

The rows are built through `app/hat/runtime.py`, written to the real tables, and the rules are
then read back with SQL over the stored rows."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.models import HatApproval, HatFill, HatOrder, HatPosition, HatSignal
from app.hat.runtime import child_is_virtual, position_is_virtual

ENGINE = memory_engine(HatSignal, HatApproval, HatOrder, HatFill, HatPosition)
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
PURPOSES = ("entry", "stop_day", "stop_gtt", "exit_partial", "exit_full", "flatten")

lifecycle = st.tuples(
    st.sampled_from(["shadow", "confirm", "auto"]),
    st.sampled_from(["take", "skip"]),  # what the user decided in confirm mode
    st.lists(st.sampled_from(PURPOSES), min_size=1, max_size=4),
    st.integers(min_value=0, max_value=3),  # fills per order
)


def write_trade(mode: str, decision: str, purposes: list[str], fills: int) -> uuid.UUID:
    """One signal, its position and orders, written the way the router and lifecycle will."""
    signal_id, position_id = uuid.uuid4(), uuid.uuid4()
    skipped = mode == "confirm" and decision == "skip"
    with Session(ENGINE) as session:
        session.add(
            HatSignal(
                id=signal_id,
                bot_id=uuid.uuid4(),
                strategy_version_id=uuid.uuid4(),
                risk_policy_version_id=uuid.uuid4(),
                playbook="default",
                instrument_id=uuid.uuid4(),
                trade_date=NOW.date(),
                triggered_at=NOW,
                trigger_price=Decimal("100"),
                stop_price=Decimal("95"),
                qty=1,
                risk_amount=Decimal("5"),
                currency="INR",
                mode=mode,
                state="open",
                risk_decision={},
            )
        )
        if mode == "confirm":
            session.add(
                HatApproval(
                    signal_id=signal_id,
                    sent_at=NOW,
                    expires_at=NOW,
                    decision=decision,
                    token_hash="h",
                )
            )
        virtual = position_is_virtual(mode, skipped=skipped)
        session.add(
            HatPosition(
                id=position_id,
                bot_id=uuid.uuid4(),
                signal_id=signal_id,
                instrument_id=uuid.uuid4(),
                is_virtual=virtual,
                qty_open=1,
                avg_entry=Decimal("100"),
                initial_stop=Decimal("95"),
                current_stop=Decimal("95"),
                currency="INR",
                opened_at=NOW,
                state="open",
                rules_fired={},
            )
        )
        for purpose in purposes:
            order_id = uuid.uuid4()
            order_virtual = child_is_virtual(virtual)
            session.add(
                HatOrder(
                    id=order_id,
                    signal_id=signal_id,
                    position_id=position_id,
                    broker_account_id=uuid.uuid4(),
                    purpose=purpose,
                    side="buy",
                    qty=1,
                    order_type="limit",
                    tag=f"HT{order_id.hex[:18]}",
                    status="filled",
                    is_virtual=order_virtual,
                    currency="INR",
                )
            )
            for _ in range(fills):
                session.add(
                    HatFill(
                        order_id=order_id,
                        qty=1,
                        price=Decimal("100"),
                        fees=Decimal("0"),
                        currency="INR",
                        filled_at=NOW,
                        is_virtual=child_is_virtual(order_virtual),
                    )
                )
        session.commit()
    return signal_id


@given(trades=st.lists(lifecycle, min_size=1, max_size=4))
def test_virtual_consistency(trades: list[tuple[str, str, list[str], int]]) -> None:
    """For every set of trades written, a fill has its order's flag, an order has its
    position's flag, and a position is virtual exactly when its signal's mode was shadow or the
    signal was skipped in confirm mode."""
    signals = [write_trade(*trade) for trade in trades]
    with Session(ENGINE) as session:
        fills_off = session.scalar(
            select(func.count())
            .select_from(HatFill)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatFill.is_virtual != HatOrder.is_virtual, HatOrder.signal_id.in_(signals))
        )
        orders_off = session.scalar(
            select(func.count())
            .select_from(HatOrder)
            .join(HatPosition, HatPosition.id == HatOrder.position_id)
            .where(HatOrder.is_virtual != HatPosition.is_virtual, HatOrder.signal_id.in_(signals))
        )
        positions = session.execute(
            select(HatPosition.is_virtual, HatSignal.mode, HatApproval.decision)
            .join(HatSignal, HatSignal.id == HatPosition.signal_id)
            .outerjoin(HatApproval, HatApproval.signal_id == HatSignal.id)
            .where(HatSignal.id.in_(signals))
        ).all()
    assert fills_off == 0
    assert orders_off == 0
    assert len(positions) == len(signals)
    for is_virtual, mode, decision in positions:
        assert is_virtual == (mode == "shadow" or (mode == "confirm" and decision == "skip"))


@given(mode=st.text(max_size=12).filter(lambda m: m not in ("shadow", "confirm", "auto")))
def test_unknown_mode_is_refused(mode: str) -> None:
    """A mode outside shadow, confirm and auto never yields a flag (no default to real)."""
    with pytest.raises(ValueError, match="unknown signal mode"):
        position_is_virtual(mode)
