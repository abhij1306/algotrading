"""PBT telegram.*: the pure parts of `app.hat.telegram`: the weekly veto digest's day, which
Bot API refusals are permanent, and the reply to a Take."""

import uuid
from datetime import date, datetime, time, timedelta

from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.engine.approvals import ApprovalDecision, Placement
from app.hat.risk.sessions import SessionCalendar
from app.hat.telegram.callbacks import take_reply
from app.hat.telegram.client import refusal_retryable
from app.hat.telegram.commands import AUTO_NOT_OWNER, AUTO_UNCONFIRMED, auto_refusal
from app.hat.telegram.reports import is_week_end
from app.hat.time import IST


def ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


DAY_KIND = st.sampled_from(("trading", "holiday", "unknown"))


@given(st.lists(DAY_KIND, min_size=14, max_size=14), st.integers(min_value=0, max_value=6))
@example(kinds=["trading"] * 5 + ["unknown"] * 9, day=4)  # a calendar that runs out on a Friday
@example(kinds=["trading"] * 3 + ["unknown"] * 11, day=2)  # ... and on a Wednesday
def test_veto_digest_on_week_end(kinds: list[str], day: int) -> None:
    """The weekly veto digest goes out on a trading day exactly when
    no later weekday of its week may still trade (a holiday in the calendar does not; a day
    the calendar does not have yet may). Saturdays and Sundays never trade. Harm: the "weekly"
    digest comes every day once the calendar runs out, or never in a week with a holiday."""
    monday = date(2026, 10, 19)
    days = [monday + timedelta(days=n) for n in range(14)]
    rows = []
    for d, kind in zip(days, kinds, strict=True):
        if kind == "unknown":
            continue
        trading = kind == "trading" and d.weekday() < 5
        opens = ist(d, 9, 15) if trading else None
        closes = ist(d, 15, 30) if trading else None
        rows.append((d, trading, opens, closes))
    calendar = SessionCalendar.from_rows(rows)
    target = days[day]

    def may_trade(d: date) -> bool:
        if d.weekday() >= 5:
            return False
        kind = kinds[days.index(d)]
        return kind != "holiday"

    is_trading = kinds[day] == "trading" and target.weekday() < 5
    rest_of_week = [d for d in days if d > target and d.isocalendar()[1] == target.isocalendar()[1]]
    expected = is_trading and not any(may_trade(d) for d in rest_of_week)
    assert is_week_end(calendar, target) is expected


def test_only_400_403_404_are_permanent() -> None:
    assert [
        s for s in (200, 400, 401, 403, 404, 409, 429, 500, 502) if not refusal_retryable(s)
    ] == [
        400,
        403,
        404,
    ]


def test_take_reply_says_what_happens_to_the_entry() -> None:
    sid = uuid.uuid4()

    def reply(outcome: str, placement: Placement | None) -> tuple[bool, str | None]:
        r = take_reply(ApprovalDecision(sid, outcome, placement=placement))
        return r.accepted, r.text

    assert reply("approved", Placement.SENT) == (True, "Take: approved, entry sent")
    assert reply("approved", Placement.RETRYING) == (
        True,
        "Take: approved, placement retrying (see alerts)",
    )
    assert reply("approved", Placement.REFUSED)[1] == "Take: approved, entry NOT placed (see alert)"
    assert reply("approved", None)[1] == "Take: approved, entry NOT placed (see alert)"
    assert reply("unknown", Placement.UNKNOWN)[0] is False
    assert reply("error", None) == (False, "Refused: error")
    assert reply("max_chase", None) == (False, "Refused: max_chase")


def test_auto_is_the_owners_switch_after_confirmation() -> None:
    """HAT-OVR-061, D210: auto only for accounts all the owner's, and only once confirmed."""
    owner, other = uuid.UUID(int=1), uuid.UUID(int=2)
    table = [
        ([owner], False, AUTO_UNCONFIRMED),
        ([owner], True, None),
        ([owner, owner], True, None),
        ([other], False, AUTO_NOT_OWNER),
        ([other], True, AUTO_NOT_OWNER),  # handed over while the confirmation was pending
        ([owner, other], True, AUTO_NOT_OWNER),
        ([owner, None], True, AUTO_NOT_OWNER),  # an account not found
        ([], True, AUTO_NOT_OWNER),
    ]
    for users, confirmed, expected in table:
        assert auto_refusal(owner, users, confirmed=confirmed) == expected, (users, confirmed)
