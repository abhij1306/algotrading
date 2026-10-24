"""The `HAT_MPP_RESEND` flag: the re-send of a market sale that SEBI market price protection
left resting is on only for a true value."""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.settings import load_settings
from app.utils.env_loader import TRUE_VALUES

# ---------------------------------------------------------------------------------------
# step 3: a market sale resting unfilled is sent again fresh


# ---------------------------------------------------------------------------------------
# (G03 live): a sale at the lower circuit waits in the queue


# ---------------------------------------------------------------------------------------
# (G22): a sale refused twice goes as a limit at the lower band


# ---------------------------------------------------------------------------------------
# a triggered GTT's sale already at the market is asked again
# ---------------------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@example(resend=None, page=None)
@example(resend="yes", page="0")
@given(
    resend=st.none() | st.sampled_from(["1", "true", " On ", "yes", "0", "off", "", "no", "maybe"]),
    page=st.none() | st.sampled_from(["1", "2", "5", "0", "-3", "x", ""]),
)
def test_resend_flag_parsed(resend: str | None, page: str | None) -> None:
    """: `HAT_MPP_RESEND` turns the re-send on exactly for a true value (off
    unset or for anything else), and `HAT_MPP_PAGE_AFTER` is a positive whole number, else 3.
    Harm: the re-send of an unconfirmed book shape is on by default, or a typo never pages."""
    env = {}
    if resend is not None:
        env["HAT_MPP_RESEND"] = resend
    if page is not None:
        env["HAT_MPP_PAGE_AFTER"] = page
    loaded = load_settings(env)
    assert loaded.mpp_resend == (resend is not None and resend.strip().lower() in TRUE_VALUES)
    number = int(page) if page is not None and page.lstrip("-").isdigit() else 0
    assert loaded.mpp_page_after == (number if number > 0 else 3)
