"""Seams of the execution path's robustness rules: an unreadable foreign trade row, a failed
symbol read, the second absent read of a lost stop or sale, and the close- and open-phase
times of a session that closes or opens at another time. Pure functions and the
adapter over a recording SDK; no broker is called."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import cast

import pytest
from hat.fakes.broker import CANCEL_TIMEOUT, FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.daily.exits import ExitViews, ShadowExits
from app.hat.data.bars import BarBuilder
from app.hat.data.gapscan import is_late
from app.hat.data.preopen import GapSource, PreopenGaps
from app.hat.engine.trigger import in_window
from app.hat.execution.broker import (
    CODE_UNAVAILABLE,
    BrokerUnavailableError,
    GttRequest,
)
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_payloads import parse_trade_book
from app.hat.execution.manage import EXITS_CLOSE_FROM, EXITS_CLOSE_UNTIL, in_close_window
from app.hat.execution.position_desk import (
    PositionDesk,
    Verdict,
    second_absence,
)
from app.hat.execution.protect import CANCEL_ACK_EVERY, CANCEL_ACK_READS, await_cancel
from app.hat.execution.tags import encode_tag
from app.hat.ops.scheduler import ScheduledJob, due, starts_at, window_end
from app.hat.risk.sessions import (
    SessionCalendar,
    before_continuous_end,
    close_shift,
    open_shift,
)
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


def test_the_close_window_ends_before_the_closing_auction() -> None:
    # continuous trading ends 15 minutes before the close; the window is 10 to 5 minutes before
    assert before_continuous_end(10) == time(15, 5)
    assert calendar(ist(DAY, 15, 30)).continuous_end_on(DAY) == ist(DAY, 15, 15)
    assert calendar(ist(DAY, 15, 30)).continuous_end_on(DAY + timedelta(days=1)) is None
    assert not in_close_window(ist(DAY, 15, 4))
    assert in_close_window(ist(DAY, 15, 5))
    assert not in_close_window(ist(DAY, 15, 10))


def test_the_close_window_moves_with_a_muhurat_session() -> None:
    shift = calendar(ist(DAY, 19, 15)).close_shift(DAY)
    assert in_close_window(ist(DAY, 15, 7))
    assert not in_close_window(ist(DAY, 15, 7), shift)
    assert in_close_window(ist(DAY, 18, 50), shift)
    assert not in_close_window(ist(DAY, 18, 55), shift)


def test_a_shifted_job_starts_and_ends_with_the_session() -> None:
    shift = calendar(ist(DAY, 14, 0)).close_shift
    job = ScheduledJob(
        "exits.close", EXITS_CLOSE_FROM, lambda _d: None, until=EXITS_CLOSE_UNTIL, shift=shift
    )
    assert starts_at(job, DAY) == ist(DAY, 13, 35)

    def is_due(at: datetime) -> bool:
        return due(job, at, trading=lambda _d: True, succeeded=(), attempted=None)

    assert not is_due(ist(DAY, 13, 34))
    assert is_due(ist(DAY, 13, 35))
    assert not is_due(ist(DAY, 13, 40))
    assert not is_due(ist(DAY, 15, 5))


def test_the_shadow_close_pass_runs_only_in_the_shifted_window() -> None:
    """Outside its window the shadow close pass returns before any read; inside it, it runs
    (here: reaches its session factory)."""

    def no_session() -> Session:
        raise LookupError("session opened")

    shadow = ShadowExits(no_session, cast("ExitViews", None), set)
    muhurat = timedelta(hours=3, minutes=45)  # an 18:00-19:15 session
    assert shadow.close(ist(DAY, 15, 7), muhurat) == 0
    assert shadow.close(ist(DAY, 18, 57), muhurat) == 0
    with pytest.raises(LookupError):
        shadow.close(ist(DAY, 18, 52), muhurat)


def test_a_settled_order_forgets_its_first_absent_read() -> None:
    clock = FixedClock(ist(DAY, 11, 0))
    desk = PositionDesk(settle_after=SETTLE, clock=clock)
    sent = ist(DAY, 10, 58)
    assert desk.verdict(sent, False, tag="C") is Verdict.WAIT
    assert desk.verdict(sent, True, tag="C", filled_qty=5) is Verdict.WAIT  # found, shares sold
    clock.advance(SETTLE)
    assert desk.verdict(sent, False, tag="C") is Verdict.WAIT  # a first read again


# --- the open phase of a session that opens at another time (Muhurat, 14:20-15:30) -------------

MUHURAT = date(2026, 11, 8)


def muhurat() -> SessionCalendar:
    return SessionCalendar.from_rows([(MUHURAT, True, ist(MUHURAT, 14, 20), ist(MUHURAT, 15, 30))])


def test_open_shift_and_hours_follow_the_session() -> None:
    assert open_shift(DAY, ist(DAY, 9, 15)) == timedelta(0)
    assert open_shift(DAY, None) == timedelta(0)
    assert muhurat().open_shift(MUHURAT) == timedelta(hours=5, minutes=5)
    assert muhurat().close_shift(MUHURAT) == timedelta(0)
    assert muhurat().hours(MUHURAT) == (time(14, 20), time(15, 30))
    assert muhurat().hours(DAY) == (time(9, 15), time(15, 30))  # unknown: the regular session


def test_the_open_jobs_start_with_a_muhurat_open() -> None:
    shift = muhurat().open_shift
    prepare = ScheduledJob("live.prepare", time(8, 45), lambda _d: None, shift=shift)
    scan = ScheduledJob("gapscan", time(9, 16), lambda _d: None, until=time(9, 20), shift=shift)
    assert starts_at(prepare, MUHURAT) == ist(MUHURAT, 13, 50)
    assert starts_at(scan, MUHURAT) == ist(MUHURAT, 14, 21)
    assert window_end(scan, MUHURAT) == ist(MUHURAT, 14, 25)
    assert is_late(ist(MUHURAT, 14, 25), MUHURAT, shift(MUHURAT))
    assert not is_late(ist(MUHURAT, 14, 24), MUHURAT, shift(MUHURAT))


def test_muhurat_bars_and_preopen_follow_the_session() -> None:
    cal = muhurat()
    builder = BarBuilder(hours=cal.hours)
    assert builder.bar_start(ist(MUHURAT, 14, 22)) == ist(MUHURAT, 14, 20)
    assert builder.bar_start(ist(MUHURAT, 14, 19)) is None
    assert builder.bar_start(ist(DAY, 9, 17)) == ist(DAY, 9, 15)
    gaps = PreopenGaps(lambda day: cal.hours(day)[0])
    tick = {"symbol": "TCS", "prev_close_price": 100, "indicative_open_price": 104}
    assert gaps.observe(tick, ist(MUHURAT, 9, 5)) is None  # the regular pre-open: shut
    found = gaps.observe(tick, ist(MUHURAT, 14, 10))
    assert found is not None
    assert found.source is GapSource.PREOPEN


def test_the_trigger_window_ends_before_the_closing_auction() -> None:
    """From 5 minutes after the open to 10 minutes before continuous trading ends (15:15)."""
    opens, closes = ist(DAY, 9, 15), ist(DAY, 15, 30)
    assert not in_window(ist(DAY, 9, 19), opens, closes)
    assert in_window(ist(DAY, 9, 20), opens, closes)
    assert in_window(ist(DAY, 15, 5), opens, closes)
    assert not in_window(ist(DAY, 15, 6), opens, closes)
    muhurat_open, muhurat_close = ist(MUHURAT, 14, 20), ist(MUHURAT, 15, 30)
    assert not in_window(ist(MUHURAT, 14, 24), muhurat_open, muhurat_close)
    assert in_window(ist(MUHURAT, 14, 25), muhurat_open, muhurat_close)
    assert not in_window(ist(MUHURAT, 15, 6), muhurat_open, muhurat_close)


# --- a sale's day stop: cancel, then the acknowledgement, then the sale -----------------------


STOP_TAG = encode_tag(uuid.UUID(int=7), "stop_day", 0)


def stop_row(status: int, filled: int = 0) -> dict[str, object]:
    return {
        "id": "S-1",
        "orderTag": STOP_TAG,
        "symbol": "NSE:TCS-EQ",
        "side": -1,
        "qty": 10,
        "filledQty": filled,
        "status": status,
        "productType": "CNC",
        "type": 3,
    }


def test_a_sale_waits_for_its_stop_cancel_to_show_in_the_book() -> None:
    """Fyers cannot modify an SL-M into a MARKET order, so the stop is cancelled first and the
    sale may go only once the book shows it cancelled: the cancel is sent before any read, the
    book is read again while it still shows the stop working, and nothing is placed meanwhile."""
    fake = FakeBroker(orders=[stop_row(6)])
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            fake.orders = [stop_row(1)]

    adapter = FyersHatBroker(fake, instruments=lambda: [("TCS", "NSE:TCS-EQ")])
    assert await_cancel(adapter, "S-1", STOP_TAG, sleep=sleep)
    assert [name for name, _ in fake.calls] == [
        "cancel_order",
        "fetch_orderbook",
        "fetch_orderbook",
        "fetch_orderbook",
    ]
    assert waits == [CANCEL_ACK_EVERY, CANCEL_ACK_EVERY]


@pytest.mark.parametrize(
    ("book", "cancel_reply"),
    [
        ([stop_row(6)], {"s": "ok", "message": "Cancelled"}),  # never shown cancelled
        ([stop_row(2, filled=10)], {"s": "error", "message": "rejected"}),  # it triggered
        (BrokerUnavailableError("down"), CANCEL_TIMEOUT),  # the answer and the book lost
    ],
)
def test_no_cancel_ack_in_the_bounded_wait_holds_the_sale(
    book: object, cancel_reply: object
) -> None:
    fake = FakeBroker(orders=book, cancel_reply=cancel_reply)
    waits: list[float] = []
    adapter = FyersHatBroker(fake, instruments=lambda: [("TCS", "NSE:TCS-EQ")])
    assert not await_cancel(adapter, "S-1", STOP_TAG, sleep=waits.append)
    assert fake.calls[0] == ("cancel_order", "S-1")
    assert not any(name == "place_order" for name, _ in fake.calls)
    assert len(waits) <= CANCEL_ACK_READS - 1


def test_no_cancel_is_sent_while_writes_are_refused() -> None:
    fake = FakeBroker(orders=[stop_row(1)], circuit_open=True)
    adapter = FyersHatBroker(fake, instruments=lambda: [("TCS", "NSE:TCS-EQ")])
    assert not await_cancel(adapter, "S-1", STOP_TAG, sleep=lambda _s: None)
    assert fake.calls == []
