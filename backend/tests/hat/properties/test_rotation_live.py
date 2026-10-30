"""A rotation's live inputs and its buy's risk check, on small hand-built inputs: `decide` under
`ROTATION_BUY`, `sizing.rotation_quantity` and the closes window the live ranking reads
(`rotation_live.closes_window`)."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from app.hat.engine.rotation_live import closes_window
from app.hat.risk.decision import ROTATION_BUY, decide
from app.hat.risk.kill import KillState
from app.hat.risk.sizing import UNBOUNDED_CASH, SizingInputError, rotation_quantity
from app.hat.time import IST
from tests.hat.hat_risk_support import TRADE_DAY, valid_ctx

D = Decimal


def _buy(**changes: object) -> object:
    """A rotation buy every check accepts: 100 shares at 100 with ample cash, no stop."""
    return decide(
        valid_ctx(
            **{
                "action": ROTATION_BUY,
                "quantity": 100,
                "stop_price": None,
                "adv20": None,
                **changes,
            }
        )
    )


def test_a_rotation_buy_takes_its_shares_with_no_stop_or_risk() -> None:
    found = _buy()
    assert found.accepted
    assert found.quantity == 100
    assert found.stop == 0
    assert found.risk_amount == 0
    assert found.binding == "rotation"
    assert found.warnings == ()


def test_a_rotation_buy_is_cut_to_the_free_cash() -> None:
    found = _buy(free_cash=D(2550))
    assert found.accepted
    assert found.quantity == 25
    assert found.warnings == ("size_bound_by_cash",)
    assert _buy(free_cash=D(99)).reason == "size_zero_cash"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"kill": KillState(account_tripped=True)}, "kill_account"),
        ({"kill": KillState(bot_tripped=True)}, "kill_bot"),
        ({"kill": KillState(health_tripped=True)}, "kill_health"),
        ({"kill": KillState(paused=True)}, "bot_paused"),
        ({"at": datetime.combine(TRADE_DAY, time(16, 0), tzinfo=IST)}, "market_closed"),
        ({"feed_stale": True}, "stale_feed"),
        ({"quality_failed": True}, "quality_failed"),
        ({"quantity": 0}, "size_zero_position"),
        ({"quantity": None}, "missing_input"),
        ({"free_cash": None}, "missing_input"),
        ({"entry_price": 100.0}, "missing_input"),
    ],
)
def test_a_rotation_buy_is_refused_as_an_entry_is(changes: dict[str, object], reason: str) -> None:
    found = _buy(**changes)
    assert not found.accepted
    assert found.reason == reason


def test_a_rotation_buy_reads_no_regime_gate_slot_or_open_risk() -> None:
    # the gate, the slots and the day's entry count bind a swing entry, never a rotation buy
    assert _buy(gate_on=False, entries_today=10_000, open_positions=None).accepted


def test_an_entry_still_needs_its_stop() -> None:
    assert decide(valid_ctx(quantity=100, stop_price=None)).reason == "missing_input"


def test_rotation_quantity_floors_to_the_cash() -> None:
    assert rotation_quantity(10, D(100), D(5000)) == (10, "rotation")
    assert rotation_quantity(10, D(100), D(999)) == (9, "cash")
    assert rotation_quantity(10, D(100), UNBOUNDED_CASH) == (10, "rotation")
    for bad in (0, -1, True):
        with pytest.raises(SizingInputError):
            rotation_quantity(bad, D(100), D(5000))  # type: ignore[arg-type]
    with pytest.raises(SizingInputError):
        rotation_quantity(1, D(0), D(5000))


def _bars(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["instrument_id", "date", "close"], orient="row")


def test_closes_window_lays_out_the_last_sessions_through_the_night() -> None:
    days = [date(2024, 1, d) for d in (1, 2, 3, 4)]
    bars = _bars(
        [
            ("a", days[0], 10.0),
            ("a", days[1], 11.0),
            ("a", days[3], 13.0),  # no bar on the 3rd
            ("b", days[1], 20.0),
            ("b", days[2], 21.0),
            ("b", days[3], 22.0),
            ("z", days[3], 99.0),  # not asked for
        ]
    )
    found = closes_window(bars, ["a", "b"], days[2], 3)  # the 4th is after the night
    assert found[:2, 0].tolist() == [10.0, 11.0]
    assert math.isnan(found[2, 0])
    assert math.isnan(found[0, 1])
    assert found[1:, 1].tolist() == [20.0, 21.0]
    # more rows than sessions: the oldest rows stay empty
    longer = closes_window(bars, ["b"], days[3], 6)
    assert np.isnan(longer[:3, 0]).all()
    assert longer[3:, 0].tolist() == [20.0, 21.0, 22.0]


def test_closes_window_has_no_column_without_names() -> None:
    bars = _bars([("a", date(2024, 1, 1), 1.0)])
    assert closes_window(bars, [], date(2024, 1, 1), 3).shape == (3, 0)
