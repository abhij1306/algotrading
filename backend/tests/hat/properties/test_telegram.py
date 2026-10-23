"""PBT telegram.* (08; ): the
Telegram service, event relay, notifier, reports and command log of `app.hat.telegram`, on
SQLite, over a recording fake of the Bot API transport (`hat_telegram_support.FakeTransport`)
and the real router over the Fyers adapter and `FakeBroker`. Nothing reaches Telegram or a
broker."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hat_support import memory_engine
from hat_telegram_support import (
    TRADE_DAY,
    TgRig,
    ist,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from app.base import Base
from app.hat.engine.approvals import ApprovalDecision, Placement
from app.hat.models import (
    HatCommandLog,
    HatJobRun,
    HatTelegramMessage,
)
from app.hat.risk.sessions import SessionCalendar
from app.hat.telegram.callbacks import take_reply
from app.hat.telegram.client import refusal_retryable
from app.hat.telegram.reports import is_week_end

ENGINE = memory_engine(HatTelegramMessage, HatCommandLog, HatJobRun)
days = st.dates(min_value=date(2024, 1, 1), max_value=date(2040, 12, 31))


def instant(day: date, age: tuple[int, int]) -> datetime:
    days_ago, hour = age
    return datetime.combine(day - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC) + (
        timedelta(hours=hour)
    )


# --- shared helpers for the properties ---------------------------------------------------

D = Decimal
# Rows the service writes for every update (its log, its message links, its cursors).
CHAT_TABLES = {"hat_command_log", "hat_telegram_message", "hat_telegram_cursor"}
EVENING = ist(TRADE_DAY, 20)  # after the close: /mode may be queued
SLOW = settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def state(rig: TgRig) -> dict[str, list[str]]:
    """Every HAT table but the chat's own rows, as sorted row reprs."""
    out = {}
    with rig.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name.startswith("hat_") and table.name not in CHAT_TABLES:
                out[table.name] = sorted(repr(tuple(row)) for row in conn.execute(table.select()))
    return out


# --- one alert per health failure -------------------------------------

# --- other chats change nothing ---------------------------------------


# --- one log row per update ------------------------------------------


# --- the closed command set ---------------------------------------------------------

# --- no order from text -------------------------------------------------------------


# --- none dropped, none applied twice --------------------------------------------


class CrashError(BaseException):
    """The process dies here: not an `Exception`, so no handler's error branch catches it."""


# What each update is: a text command, a message from another chat, a command whose handler
# raises (the error row), one that fails before any handler runs (`poison`: its refusal cannot
# be written), one whose first try meets a database error that passes (`flaky`), or the Take of
# a card (only the first one drawn; later ones are text).
KINDS = ("text", "text", "stranger", "failing", "poison", "flaky", "take")


# --- user events are attributed -----------------------------------------------------

# --- no secret, token or broker text in output --------------------------------------


# --- only the allowlisted chat ------------------------------------------------------


# --- login prompts ------------------------------------------------------------------


# --- the watchlist digest -----------------------------------------------------------

POOL = tuple(f"W{n:02d}" for n in range(14))
# --- the card is the signal ---------------------------------------------------------


# --- reports -----------------------------------------------------------

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


# --- /mode only outside market hours ------------------------------------------------


# --- /mode names one bot and says which gate refuses it ----------------------------


# --- second confirmation ------------------------------------------------------------


# --- flatten -----------------------------------------------------------


# --- read commands ------------------------------------------------------------------


# --- forged tokens ------------------------------------------------------------------

# --- the 5-minute window ------------------------------------------------------------


# --- Take re-checks max-chase -------------------------------------------------------


# --- the raw token is never kept ---------------------------------------


# --- a refusal that will not pass; the answer to a Take ----------------------------


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
