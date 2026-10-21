"""The FakeBroker lives in `tests/hat/fakes/broker.py` (K1); the legacy suite imports it from there
so there is one fake. Use the `fake_broker` fixture (tests/conftest.py) to put it behind every
broker handle the app holds. Delete this module with the legacy suite (K4/K5)."""

from hat.fakes.broker import (
    DEFAULT_FUNDS,
    DEFAULT_LTP,
    FakeBroker,
    accepted,
    broker_position,
    rejected,
    unconfirmed,
)

__all__ = [
    "DEFAULT_FUNDS",
    "DEFAULT_LTP",
    "FakeBroker",
    "accepted",
    "broker_position",
    "rejected",
    "unconfirmed",
]
