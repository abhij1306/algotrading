"""`position_is_virtual` refuses a mode outside shadow, confirm and auto (no default to real)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.hat.governance import MODE_VALUES
from app.hat.runtime import position_is_virtual


@given(mode=st.text(max_size=12).filter(lambda m: m not in MODE_VALUES))
def test_unknown_mode_is_refused(mode: str) -> None:
    """A mode outside shadow, confirm and auto never yields a flag (no default to real)."""
    with pytest.raises(ValueError, match="unknown signal mode"):
        position_is_virtual(mode)
