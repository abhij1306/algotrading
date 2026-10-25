"""Seams of the execution path's robustness rules: an unreadable foreign trade row, a failed
symbol read, the second absent read of a lost stop or sale,
and the close-phase times of a session that closes at another time. Pure functions and the
adapter over a recording SDK; no broker is called."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from hat.fakes.fyers_sdk import RecordingSdk, broker_over

from app.hat.clock import FixedClock
from app.hat.execution.broker import (
    CODE_UNAVAILABLE,
    GttRequest,
)
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_payloads import parse_trade_book
from app.hat.execution.manage import in_close_window
from app.hat.execution.position_desk import (
    PositionDesk,
    Verdict,
    second_absence,
)
from app.hat.execution.tags import encode_tag
from app.hat.ops.scheduler import ScheduledJob, due, starts_at
from app.hat.risk.sessions import SessionCalendar, close_shift
from app.hat.time import IST

DAY = date(2026, 11, 9)
SETTLE = timedelta(seconds=60)


def ist(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def trade_row(tag: str | None, price: object = 101.5) -> dict[str, object]:
    return {
        "tradeNumber": "T1",
        "orderNumber": "O1",
        "orderTag": tag,
        "symbol": "NSE:TCS-EQ",
        "side": 1,
        "tradedQty": 5,
        "tradePrice": price,
    }


# --- an unreadable foreign trade row ----------------------------------------------------------


def test_an_unreadable_foreign_trade_is_skipped_and_counted() -> None:
    tag = encode_tag(uuid.UUID(int=1), "entry", 0)
    book = parse_trade_book([trade_row(tag), trade_row(None, 0), trade_row("1:MANUAL", None)])
    assert [t.tag for t in book] == [tag]
    assert book.skipped == 2


@pytest.mark.parametrize(
    "tag",
    [
        encode_tag(uuid.UUID(int=1), "entry", 0),
        "1:GTT" + encode_tag(uuid.UUID(int=1), "stop_gtt", 0),
    ],
)
def test_an_unreadable_hat_trade_fails_the_read(tag: str) -> None:
    with pytest.raises(ValueError):
        parse_trade_book([trade_row(tag, 0)])


# --- no symbol guessed --------------------------------------------------------------------------


def unreadable() -> list[tuple[str, str]]:
    raise RuntimeError("database down")


def test_a_failed_symbol_read_refuses_a_gtt_unsent() -> None:
    """As an order (`test_tags`): a GTT is never sent under a guessed `-EQ` symbol."""
    sdk = RecordingSdk()
    adapter = FyersHatBroker(broker_over(sdk), instruments=unreadable)
    tag = encode_tag(uuid.UUID(int=2), "stop_gtt", 0)
    gtt = adapter.place_gtt_stop(GttRequest("TCS", 5, Decimal("90"), Decimal("89"), tag=tag))
    assert (gtt.status, gtt.code, sdk.calls) == ("rejected", CODE_UNAVAILABLE, [])


# --- the second absent read of a lost stop or sale -------------------------------


def test_second_absence_needs_the_settle_wait() -> None:
    first = ist(DAY, 11, 0)
    assert not second_absence(first, first, SETTLE)
    assert not second_absence(first, first + SETTLE - timedelta(seconds=1), SETTLE)
    assert second_absence(first, first + SETTLE, SETTLE)


def test_a_lost_stop_expires_only_on_a_second_absent_read() -> None:
    clock = FixedClock(ist(DAY, 11, 0))
    desk = PositionDesk(settle_after=SETTLE, clock=clock)
    sent = ist(DAY, 10, 58)
    assert desk.verdict(sent, False, tag="A") is Verdict.WAIT  # the first absent read
    clock.advance(SETTLE)
    assert desk.verdict(sent, False, tag="A") is Verdict.EXPIRE
    # a read that finds it forgets the first absence
    assert desk.verdict(sent, False, tag="B") is Verdict.WAIT
    assert desk.verdict(sent, True, tag="B") is Verdict.ADOPT
    clock.advance(SETTLE)
    assert desk.verdict(sent, False, tag="B") is Verdict.WAIT


# --- the close phase of a session that closes at another time ----------------------------------


def calendar(close: datetime) -> SessionCalendar:
    return SessionCalendar.from_rows([(DAY, True, close - timedelta(hours=1), close)])


def test_close_shift_follows_the_session_close() -> None:
    assert close_shift(DAY, ist(DAY, 15, 30)) == timedelta(0)
    assert close_shift(DAY, ist(DAY, 14, 0)) == timedelta(minutes=-90)
    assert close_shift(DAY, None) == timedelta(0)
    assert calendar(ist(DAY, 19, 15)).close_shift(DAY) == timedelta(hours=3, minutes=45)
    assert calendar(ist(DAY, 19, 15)).close_shift(DAY + timedelta(days=1)) == timedelta(0)


def test_the_close_window_moves_with_a_muhurat_session() -> None:
    shift = calendar(ist(DAY, 19, 15)).close_shift(DAY)
    assert in_close_window(ist(DAY, 15, 22))
    assert not in_close_window(ist(DAY, 15, 22), shift)
    assert in_close_window(ist(DAY, 19, 5), shift)
    assert not in_close_window(ist(DAY, 19, 10), shift)


def test_a_shifted_job_starts_and_ends_with_the_session() -> None:
    shift = calendar(ist(DAY, 14, 0)).close_shift
    job = ScheduledJob(
        "exits.close", time(15, 20), lambda _d: None, until=time(15, 25), shift=shift
    )
    assert starts_at(job, DAY) == ist(DAY, 13, 50)

    def is_due(at: datetime) -> bool:
        return due(job, at, trading=lambda _d: True, succeeded=(), attempted=None)

    assert not is_due(ist(DAY, 13, 49))
    assert is_due(ist(DAY, 13, 50))
    assert not is_due(ist(DAY, 13, 55))
    assert not is_due(ist(DAY, 15, 20))
