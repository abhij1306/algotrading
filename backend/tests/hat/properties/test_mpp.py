"""PBT mpp.*: HAT's sales under SEBI market
price protection, which turns a market sale into a protected limit that can rest unfilled. The
real fill sync and trade manager on SQLite, through the real Fyers adapter over `FakeBroker`; the
order-book shape of a protected sale is the assumed fixture `fyers_mpp_sale_assumed.json` (the
O2(c) live check confirms it). Nothing reaches a real broker."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.models import HatEvent, HatOrder
from app.hat.settings import load_settings
from app.utils.env_loader import TRUE_VALUES

D = Decimal
SHAPES: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fyers_mpp_sale_assumed.json").read_text("utf-8")
)["shapes"]
WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "cancel_gtt")


def _events(session: Session, rows: list[HatOrder], kind: str) -> int:
    ids = [row.id for row in rows]
    return len(
        session.scalars(
            select(HatEvent.id).where(HatEvent.aggregate_id.in_(ids), HatEvent.event_type == kind)
        ).all()
    )


# ---------------------------------------------------------------------------------------
# step 3: a market sale resting unfilled is sent again fresh
# ---------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------
# (G03 live): a sale at the lower circuit waits in the queue
# ---------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------
# (G22): a sale refused twice goes as a limit at the lower band
# ---------------------------------------------------------------------------------------


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
