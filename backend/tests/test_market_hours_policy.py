"""MarketHoursPolicy: the NSE session window in IST, weekends, holidays and DEV_MODE."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.services import live_market_service as lms
from app.utils import market_hours
from app.utils.clock import FixedClock
from app.utils.market_hours import IST, MarketHoursPolicy, MarketPhase

from fakes.market import FreezeMarket

# IST is UTC+05:30, so 09:15 IST is 03:45 UTC and 15:30 IST is 10:00 UTC.
# 2025-01-06 is a Monday.
MONDAY = (2025, 1, 6)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (utc(*MONDAY, 3, 44, 59), MarketPhase.PRE_MARKET),  # 09:14:59 IST
        (utc(*MONDAY, 3, 45, 0), MarketPhase.OPEN),  # 09:15:00 IST: open
        (utc(*MONDAY, 6, 30), MarketPhase.OPEN),  # 12:00 IST
        (utc(*MONDAY, 9, 59, 59), MarketPhase.OPEN),  # 15:29:59 IST
        (utc(*MONDAY, 10, 0, 0), MarketPhase.OPEN),  # 15:30:00 IST exactly: still open
        (utc(*MONDAY, 10, 0, 0, 1), MarketPhase.POST_MARKET),  # 15:30:00.000001 IST
        (utc(*MONDAY, 10, 0, 1), MarketPhase.POST_MARKET),  # 15:30:01 IST
        (utc(2025, 1, 5, 20, 0), MarketPhase.PRE_MARKET),  # Sun 20:00 UTC = Mon 01:30 IST
        (utc(2025, 1, 10, 20, 0), MarketPhase.WEEKEND),  # Fri 20:00 UTC = Sat 01:30 IST
        (utc(2025, 1, 11, 6, 0), MarketPhase.WEEKEND),  # Saturday 11:30 IST
        (utc(2025, 1, 12, 6, 0), MarketPhase.WEEKEND),  # Sunday 11:30 IST
        (datetime(2025, 1, 6, 9, 15, tzinfo=IST), MarketPhase.OPEN),
        (datetime(2025, 1, 6, 15, 31, tzinfo=IST), MarketPhase.POST_MARKET),
    ],
)
def test_phase_at_instants_around_the_ist_session(instant: datetime, expected: MarketPhase) -> None:
    policy = MarketHoursPolicy(clock=FixedClock(instant))

    assert policy.phase(dev_mode=False) == expected
    assert policy.phase(instant, dev_mode=False) == expected
    assert policy.is_open(dev_mode=False) is (expected == MarketPhase.OPEN)


@pytest.mark.parametrize(
    "instant", [utc(2025, 1, 11, 6, 0), utc(*MONDAY, 2, 0), utc(*MONDAY, 12, 0)]
)
def test_dev_mode_is_open_at_any_time(instant: datetime) -> None:
    policy = MarketHoursPolicy(clock=FixedClock(instant))

    assert policy.phase(dev_mode=True) == MarketPhase.DEV
    assert policy.is_open(dev_mode=True) is True


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("true", MarketPhase.DEV),
        ("TRUE", MarketPhase.DEV),
        ("false", MarketPhase.WEEKEND),
        ("1", MarketPhase.WEEKEND),
        ("yes", MarketPhase.WEEKEND),
    ],
)
def test_dev_mode_defaults_to_the_env_var_and_only_true_counts(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: MarketPhase
) -> None:
    monkeypatch.setenv("DEV_MODE", env_value)
    policy = MarketHoursPolicy(clock=FixedClock(utc(2025, 1, 11, 6, 0)))

    assert policy.phase() == expected


def test_unset_dev_mode_env_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEV_MODE", raising=False)
    policy = MarketHoursPolicy(clock=FixedClock(utc(2025, 1, 11, 6, 0)))

    assert policy.phase() == MarketPhase.WEEKEND


def test_holidays_default_to_none_and_close_a_listed_ist_date() -> None:
    # 2025-01-27 05:00 UTC is Monday 10:30 IST.
    instant = utc(2025, 1, 27, 5, 0)

    assert MarketHoursPolicy(clock=FixedClock(instant)).phase(dev_mode=False) == MarketPhase.OPEN
    closed = MarketHoursPolicy(clock=FixedClock(instant), holidays=frozenset({date(2025, 1, 27)}))
    assert closed.phase(dev_mode=False) == MarketPhase.HOLIDAY
    assert closed.is_open(dev_mode=False) is False


def test_holiday_matches_the_ist_date_not_the_utc_date() -> None:
    # 2025-01-27 20:00 UTC is already 2025-01-28 01:30 IST (pre-market on the 28th).
    policy = MarketHoursPolicy(
        clock=FixedClock(utc(2025, 1, 27, 20, 0)), holidays=frozenset({date(2025, 1, 28)})
    )

    assert policy.phase(dev_mode=False) == MarketPhase.HOLIDAY


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        MarketHoursPolicy().phase(datetime(2025, 1, 6, 10, 0), dev_mode=False)
    with pytest.raises(ValueError):
        FixedClock(datetime(2025, 1, 6, 10, 0))


def test_fixed_clock_moves_only_when_told() -> None:
    clock = FixedClock(utc(*MONDAY, 3, 44))
    policy = MarketHoursPolicy(clock=clock)
    assert policy.is_open(dev_mode=False) is False

    clock.advance(timedelta(minutes=1))
    assert policy.is_open(dev_mode=False) is True

    clock.set(utc(*MONDAY, 10, 1))
    assert policy.is_open(dev_mode=False) is False


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (utc(*MONDAY, 3, 0), (False, "Market closed (Pre-market)")),
        (utc(*MONDAY, 5, 0), (True, "Market open")),
        (utc(*MONDAY, 11, 0), (False, "Market closed (Post-market)")),
        (utc(2025, 1, 11, 5, 0), (False, "Market closed (Weekend)")),
    ],
)
def test_is_market_open_messages(
    monkeypatch: pytest.MonkeyPatch, instant: datetime, expected: tuple[bool, str]
) -> None:
    monkeypatch.setenv("DEV_MODE", "false")
    policy = MarketHoursPolicy(clock=FixedClock(instant))

    assert market_hours.is_market_open(policy) == expected


def test_is_market_open_message_in_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")

    assert market_hours.is_market_open() == (True, "Market open (DEV_MODE)")


def test_market_status_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "false")
    policy = MarketHoursPolicy(clock=FixedClock(utc(*MONDAY, 5, 0, 7)))

    assert market_hours.get_market_status(policy) == {
        "is_open": True,
        "message": "Market open",
        "current_time_ist": "10:30:07 IST",
        "current_day": "Monday",
        "market_open_time": "09:15 IST",
        "market_close_time": "15:30 IST",
    }


# --- LiveMarketService reads the injected clock --------------------------------


@pytest.mark.parametrize(
    ("instant", "is_open", "status"),
    [
        (utc(*MONDAY, 3, 45), True, "OPEN"),
        (utc(*MONDAY, 3, 44, 59), False, "CLOSED_OFF_HOURS"),
        (utc(*MONDAY, 10, 0, 1), False, "CLOSED_OFF_HOURS"),
        (utc(2025, 1, 11, 5, 0), False, "CLOSED_WEEKEND"),
    ],
)
def test_live_service_market_status_follows_its_clock(
    instant: datetime, is_open: bool, status: str
) -> None:
    live = lms.LiveMarketService(clock=FixedClock(instant))
    live.dev_mode = False

    assert live.is_market_open() is is_open
    assert live._market_status == status


def test_live_service_dev_mode_overrides_the_clock() -> None:
    live = lms.LiveMarketService(clock=FixedClock(utc(2025, 1, 11, 5, 0)))
    live.dev_mode = True

    assert live.is_market_open() is True
    assert live._market_status == "OPEN (DEV)"


def test_freeze_market_drives_the_module_helpers(freeze_market: FreezeMarket) -> None:
    @market_hours.require_market_open()
    def guarded() -> str:
        return "ran"

    clock = freeze_market(False)
    closed = market_hours.is_market_open()
    with pytest.raises(HTTPException) as raised:
        guarded()

    clock.set(datetime(2026, 10, 20, 15, 30, tzinfo=IST))  # the last open instant
    opened = (market_hours.is_market_open(), guarded())

    assert closed == (False, "Market closed (Weekend)")
    assert raised.value.status_code == 503
    assert opened == ((True, "Market open"), "ran")
