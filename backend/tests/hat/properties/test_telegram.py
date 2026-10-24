"""PBT telegram.*: the pure parts of `app.hat.telegram`: the weekly veto digest's day, which
Bot API refusals are permanent, the reply to a Take, and the approval tokens' signer."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.engine.approvals import ApprovalDecision, Placement
from app.hat.errors import HatConfigError
from app.hat.risk.kill import ARMED, TRIPPED, KillRow
from app.hat.risk.sessions import SessionCalendar
from app.hat.telegram import render
from app.hat.telegram.callbacks import take_reply
from app.hat.telegram.client import MAX_TEXT, refusal_retryable
from app.hat.telegram.commands import (
    AUTO_NOT_OWNER,
    AUTO_UNCONFIRMED,
    KILL_CHANGED,
    arm_fits,
    arm_refusal,
    auto_refusal,
    tripped_kills,
)
from app.hat.telegram.config import MIN_SECRET_BYTES
from app.hat.telegram.reports import is_week_end
from app.hat.telegram.tokens import SKIP, TAKE, ApprovalSigner, ButtonClaim
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


T0 = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)


def test_arm_rearms_only_the_kills_it_listed() -> None:
    """`/arm` lists the latest trip of each tripped drawdown or manual kill (a re-armed one, a
    health kill and an older trip are not listed), and its Confirm re-arms only while the
    tripped set is exactly the listed one. Harm: undoing a manual kill re-arms a drawdown kill
    the owner never saw, or one that tripped after the question, and restarts its peak."""
    bot_b, bot_c = uuid.uuid4(), uuid.uuid4()
    later = T0 + timedelta(hours=2)
    rows = [
        KillRow("account", None, TRIPPED, T0, "drawdown_account"),
        KillRow("account", None, ARMED, T0, "drawdown_account", rearmed_at=T0 + timedelta(hours=1)),
        KillRow("account", None, TRIPPED, later, "manual"),
        KillRow("bot", bot_b, TRIPPED, T0, "drawdown_bot"),
        KillRow("bot", bot_c, TRIPPED, T0, "drawdown_bot"),
        KillRow("bot", bot_c, ARMED, T0, "drawdown_bot", rearmed_at=later),
        KillRow("bot", bot_c, TRIPPED, later, "feed_health"),
    ]
    listed = tripped_kills(rows)
    assert listed == [rows[2], rows[3]]
    assert arm_refusal(listed, tripped_kills(rows)) is None
    # a kill tripped after the question
    newer = [*rows, KillRow("bot", bot_c, TRIPPED, later + timedelta(minutes=1), "drawdown_bot")]
    assert arm_refusal(listed, tripped_kills(newer)) == KILL_CHANGED
    # a listed kill re-armed and tripped again since
    again = [
        *rows,
        KillRow("bot", bot_b, ARMED, T0, "drawdown_bot", rearmed_at=later),
        KillRow("bot", bot_b, TRIPPED, later + timedelta(minutes=1), "drawdown_bot"),
    ]
    assert arm_refusal(listed, tripped_kills(again)) == KILL_CHANGED
    # one of them re-armed elsewhere: the set changed, nothing is re-armed
    assert arm_refusal(listed, [rows[2]]) == KILL_CHANGED
    assert arm_refusal(listed, []) == "nothing_tripped"


def test_arm_refuses_a_list_one_message_cannot_hold() -> None:
    """The `/arm` question is offered only when every kill it lists fits one Telegram message
    (`MAX_TEXT`). Harm: a cut list keeps its Confirm and re-arms kills the owner never saw."""
    line = render.kill_line("x" * 40, "drawdown_bot", T0)
    assert arm_fits([line] * 10, EXPIRES)
    fit = max(n for n in range(200) if arm_fits([line] * n, EXPIRES))
    assert len(render.confirm_text("arm", EXPIRES, [line] * fit)) <= MAX_TEXT
    assert len(render.confirm_text("arm", EXPIRES, [line] * (fit + 1))) > MAX_TEXT


SIGNER = ApprovalSigner(b"s" * MIN_SECRET_BYTES)
EXPIRES = datetime(2026, 10, 26, 4, 5, tzinfo=UTC)


def test_approval_signer_sign_verify_tamper_expiry() -> None:
    """A Take or Skip button verifies under the secret that signed it and binds its signal and
    expiry; a changed character, another secret, another expiry or another card's hash is
    refused. Harm: a forged or replayed button places a real entry."""
    signal = uuid.uuid4()
    stored = SIGNER.card_hash(signal, EXPIRES)
    for action in (TAKE, SKIP):
        data = SIGNER.button(action, signal, EXPIRES)
        assert len(data.encode()) <= 64  # Telegram's callback data limit
        claim = SIGNER.verify(data)
        assert claim == ButtonClaim(action, signal, int(EXPIRES.timestamp()))
        assert SIGNER.matches(claim, EXPIRES, stored)
        # expiry: the claim binds the approval's own expires_at, never a later one
        assert not SIGNER.matches(claim, EXPIRES + timedelta(minutes=5), stored)
        assert not SIGNER.matches(claim, EXPIRES, SIGNER.card_hash(uuid.uuid4(), EXPIRES))
        # tamper: each body character changed (the last one also carries base64 pad bits, so a
        # change there may decode to the same bytes: never to another claim), the action
        # swapped, another secret
        for at in range(1, len(data)):
            changed = data[:at] + ("A" if data[at] != "A" else "B") + data[at + 1 :]
            assert SIGNER.verify(changed) in (None, claim)
            if at < len(data) - 1:
                assert SIGNER.verify(changed) is None
        other = SKIP if action == TAKE else TAKE
        assert SIGNER.verify(other + data[1:]) is None
        assert ApprovalSigner(b"x" * MIN_SECRET_BYTES).verify(data) is None
    assert SIGNER.verify("") is None
    assert SIGNER.verify("x" + SIGNER.button(TAKE, signal, EXPIRES)[1:]) is None
    with pytest.raises(HatConfigError):
        ApprovalSigner(b"short")
