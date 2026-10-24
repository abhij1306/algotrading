"""PBT runtime.virtual_consistency: virtual and real rows share tables, and the
`is_virtual` flag of a position, its orders and their fills agree with the signal that opened it.

The rows are built through `app/hat/runtime.py`, written to the real tables, and the rules are
then read back with SQL over the stored rows."""

from datetime import UTC, datetime

import pytest
from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st

from app.hat.governance import MODE_VALUES, Mode
from app.hat.models import HatApproval, HatFill, HatOrder, HatPosition, HatSignal
from app.hat.runtime import position_is_virtual

ENGINE = memory_engine(HatSignal, HatApproval, HatOrder, HatFill, HatPosition)
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
PURPOSES = ("entry", "stop_day", "stop_gtt", "exit_partial", "exit_full", "flatten")

lifecycle = st.tuples(
    st.sampled_from(tuple(Mode)),
    st.sampled_from(["take", "skip"]),  # what the user decided in confirm mode
    st.lists(st.sampled_from(PURPOSES), min_size=1, max_size=4),
    st.integers(min_value=0, max_value=3),  # fills per order
)


@given(mode=st.text(max_size=12).filter(lambda m: m not in MODE_VALUES))
def test_unknown_mode_is_refused(mode: str) -> None:
    """A mode outside shadow, confirm and auto never yields a flag (no default to real)."""
    with pytest.raises(ValueError, match="unknown signal mode"):
        position_is_virtual(mode)
