"""Kite (Zerodha) payloads, reply parsers and mappings, on fixtures in the shapes of Kite Connect
v3's published API documentation (`fixtures/kite_replies.json`): every intent becomes the exact
SDK keyword arguments, every reply is read exactly or fails the read, every SDK error maps to
one fixed outcome. No Kite endpoint is called."""

import csv
import io
import json
import uuid
import zipfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.data.bhavcopy import nse_tokens
from app.hat.data.kite_symbols import (
    KiteListing,
    dump_rows,
    kite_listings,
    kite_symbol,
    plausible_refresh,
)
from app.hat.errors import HatConfigError
from app.hat.execution.broker import (
    CODE_OK,
    CODE_REJECTED,
    CODE_SESSION_EXPIRED,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    BrokerUnavailableError,
    GttRequest,
    InvalidIntentError,
    OrderIntent,
    OrderType,
    Side,
)
from app.hat.execution.fyers_auth import TokenRejectedError
from app.hat.execution.kite_adapter import KiteHatBroker
from app.hat.execution.kite_auth import session_reply_fields
from app.hat.execution.kite_broker import (
    EXPIRED,
    NOT_SENT,
    OK,
    RATE_LIMITED,
    REFUSED,
    UNKNOWN,
    KiteReply,
    kite_failure,
)
from app.hat.execution.kite_payloads import (
    ack_result,
    gtt_modify_params,
    gtt_params,
    kite_db_symbol,
    ltp_key,
    map_kite_status,
    order_modify_params,
    order_params,
    parse_funds,
    parse_gtt_row,
    parse_holdings,
    parse_ltp,
    parse_order_book,
    parse_positions,
    parse_trades,
    place_result,
    rows_of,
    stop_is_below,
)
from app.hat.execution.kite_tokens import kite_token_expiry
from app.hat.execution.tags import encode_tag
from app.hat.settings import active_broker
from app.hat.time import IST

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kite_replies.json").read_text("utf-8")
)
SIGNAL = uuid.UUID("6f1c2a7e-3b9d-4c51-9a0e-2f4b8d7c1e05")
ENTRY_TAG = encode_tag(SIGNAL, "entry", 0)
GTT_TAG = encode_tag(SIGNAL, "stop_gtt", 0)
same = str  # the database symbol of a trading symbol in these tests


def _intent(order_type: OrderType, **kw: object) -> OrderIntent:
    fields: dict[str, object] = {
        "symbol": "INFY",
        "side": Side.BUY,
        "qty": 3,
        "order_type": order_type,
        "tag": ENTRY_TAG,
    }
    fields.update(kw)
    return OrderIntent(**fields)  # type: ignore[arg-type]


# --- payloads -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order_type", "prices", "expected"),
    [
        (OrderType.LIMIT, {"price": Decimal("1404.05")}, {"price": 1404.05}),
        (OrderType.MARKET, {}, {"market_protection": -1}),
        (
            OrderType.SL,
            {"price": Decimal("1290"), "trigger_price": Decimal("1300")},
            {"price": 1290.0, "trigger_price": 1300.0},
        ),
        (
            OrderType.SL_M,
            {"trigger_price": Decimal("1300")},
            {"trigger_price": 1300.0, "market_protection": -1},
        ),
    ],
)
def test_order_params_exact(order_type: OrderType, prices: dict, expected: dict) -> None:
    """Each order type becomes exactly one set of `place_order` keyword arguments."""
    params = order_params(_intent(order_type, **prices), lambda s: f"{s}-BE")
    assert params == {
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "INFY-BE",
        "transaction_type": "BUY",
        "quantity": 3,
        "product": "CNC",
        "order_type": {"SL_M": "SL-M"}.get(order_type.name, order_type.name),
        "validity": "DAY",
        "tag": ENTRY_TAG,
        **expected,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"qty": 0},
        {"qty": True},
        {"product": "MIS"},
        {"tag": "MYTAG"},
        {"order_type": OrderType.LIMIT},  # a limit without its price
        {"order_type": OrderType.MARKET, "price": Decimal("10")},  # a market order with one
        {"order_type": OrderType.LIMIT, "price": Decimal("-1")},
        {"order_type": OrderType.LIMIT, "price": 10.5},  # a float price is refused
        {"order_type": OrderType.LIMIT, "price": Decimal("NaN")},
    ],
)
def test_order_params_refuses(bad: dict) -> None:
    intent = _intent(**{"order_type": OrderType.MARKET, **bad})  # type: ignore[arg-type]
    with pytest.raises(InvalidIntentError):
        order_params(intent, same)


def test_order_modify_params() -> None:
    assert order_modify_params("LIMIT", 4, Decimal("101.5"), None) == {
        "order_type": "LIMIT",
        "quantity": 4,
        "price": 101.5,
    }
    assert order_modify_params("SL-M", None, None, Decimal("95")) == {
        "order_type": "SL-M",
        "trigger_price": 95.0,
        "market_protection": -1,
    }
    assert order_modify_params("SL", None, None, Decimal("95")) is None  # no limit: not sent
    one = Decimal("1")
    with pytest.raises(InvalidIntentError):
        order_modify_params("LIMIT", 0, one, None)


def test_gtt_params_single_leg_sell_stop() -> None:
    """A protective stop is one single-leg CNC LIMIT SELL GTT placed against the last price; its
    tag is checked but not sent (Kite GTTs carry none)."""
    request = GttRequest(
        symbol="INFY",
        qty=1,
        trigger_price=Decimal("702"),
        limit_price=Decimal("702.5") - 1,
        tag=GTT_TAG,
    )
    params = gtt_params(request, same, Decimal("798"))
    assert params == {
        "trigger_type": "single",
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "trigger_values": [702.0],
        "last_price": 798.0,
        "orders": [
            {
                "transaction_type": "SELL",
                "quantity": 1,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": 701.5,
            }
        ],
    }
    assert gtt_modify_params("INFY", 1, Decimal("702"), Decimal("701.5"), Decimal("798")) == params


@pytest.mark.parametrize(
    ("change", "last"),
    [
        ({"tag": ENTRY_TAG}, "798"),  # not a stop_gtt tag
        ({"tag": None}, "798"),
        ({"side": Side.BUY}, "798"),
        ({"product": "MIS"}, "798"),
        ({"limit_price": Decimal("703")}, "798"),  # a limit above the trigger
        ({}, "702"),  # the trigger is not below the last price: a trigger on a rise
        ({}, "650"),
    ],
)
def test_gtt_params_refuses(change: dict, last: str) -> None:
    fields = {
        "symbol": "INFY",
        "qty": 1,
        "trigger_price": Decimal("702"),
        "limit_price": Decimal("701.5"),
        "tag": GTT_TAG,
        **change,
    }
    request, last_price = GttRequest(**fields), Decimal(last)  # type: ignore[arg-type]
    with pytest.raises(InvalidIntentError):
        gtt_params(request, same, last_price)


def test_ltp() -> None:
    assert ltp_key("INFY") == "NSE:INFY"
    assert parse_ltp(FIXTURE["ltp"], "NSE:INFY") == Decimal("1074.35")
    with pytest.raises(KeyError):
        parse_ltp(FIXTURE["ltp"], "NSE:SBIN")


# --- replies ------------------------------------------------------------------------------------


def test_order_book() -> None:
    book = parse_order_book(FIXTURE["orders"], same)
    assert book.skipped == 0
    by_id = {o.order_id: o for o in book}
    entry = by_id["151220000000000"]
    assert (entry.tag, entry.status, entry.qty, entry.filled_qty) == (ENTRY_TAG, "filled", 1, 1)
    assert entry.price == Decimal("1404")
    assert entry.order_type is OrderType.LIMIT
    stop = by_id["151220000000001"]
    assert (stop.status, stop.side, stop.order_type) == (
        "partially_filled",
        Side.SELL,
        OrderType.SL,
    )
    assert by_id["151220000000002"].status == "rejected"
    assert by_id["151220000000002"].tag is None
    sale = by_id["151220000000003"]  # a sale under a GTT's tag is that GTT's
    assert (sale.tag, sale.gtt_tag, sale.price) == (None, GTT_TAG, None)


def test_order_book_unreadable_rows() -> None:
    """An unreadable foreign row is skipped and counted; an unreadable HT-tagged row fails."""
    foreign = {**FIXTURE["orders"][2], "quantity": "many"}
    book = parse_order_book([*FIXTURE["orders"], foreign], same)
    assert (len(book), book.skipped) == (4, 1)
    ours = {**FIXTURE["orders"][0], "quantity": None}
    with pytest.raises(ValueError):
        parse_order_book([ours], same)
    with pytest.raises(TypeError):
        rows_of({"data": []})  # an answer without its list is never an empty book


@pytest.mark.parametrize(
    ("status", "filled", "qty", "expected"),
    [
        ("COMPLETE", 1, 1, "filled"),
        ("CANCELLED", 0, 1, "cancelled"),
        ("REJECTED", 0, 1, "rejected"),
        ("OPEN", 0, 5, "placed"),
        ("OPEN", 2, 5, "partially_filled"),
        ("TRIGGER PENDING", 0, 5, "placed"),
        ("PUT ORDER REQ RECEIVED", 0, 5, "placed"),
        ("SOMETHING NEW", 0, 5, "unconfirmed"),
        (None, 0, 5, "unconfirmed"),
    ],
)
def test_map_kite_status(status: object, filled: int, qty: int, expected: str) -> None:
    assert map_kite_status(status, filled, qty) == expected


def test_trades_take_their_order_tags() -> None:
    trades = parse_trades(FIXTURE["trades"], FIXTURE["orders"], same)
    assert [(t.order_id, t.tag, t.gtt_tag, t.qty, t.price) for t in trades] == [
        ("151220000000000", ENTRY_TAG, None, 1, Decimal("1404")),
        ("151220000000003", None, GTT_TAG, 5, Decimal("420.65")),
    ]
    with pytest.raises(KeyError):  # a trade whose order is not in the book could be HAT's
        parse_trades(FIXTURE["trades"], FIXTURE["orders"][:1], same)


def test_positions_holdings_funds() -> None:
    positions = parse_positions(FIXTURE["positions"], same)
    assert [(p.symbol, p.qty, p.avg_price, p.product) for p in positions] == [
        ("INFY", 1, Decimal("1404"), "CNC")  # SBIN's net 0 is no position
    ]
    holdings = parse_holdings(FIXTURE["holdings"], same)
    assert [(h.symbol, h.qty, h.avg_price) for h in holdings] == [
        ("GOLDBEES", 2, Decimal("40.67")),
        ("IDEA", 8, Decimal("8.466")),  # settled 5 plus 3 T1
    ]
    funds = parse_funds(FIXTURE["margins_equity"])
    assert (funds.available, funds.used, funds.total) == (
        Decimal("99725.05"),
        Decimal("145706.55"),
        Decimal("245431.60"),
    )
    with pytest.raises(KeyError):
        parse_funds({"enabled": True})  # not a reply of 0 cash


def test_gtt_rows() -> None:
    """A single-leg CNC GTT that rests or triggered is kept (untagged); a two-leg one or one that
    ended is not HAT's; an unknown status fails the read."""
    single, two_leg, cancelled = FIXTURE["gtts"]
    gtt = parse_gtt_row(single, same)
    assert gtt is not None
    assert (gtt.gtt_id, gtt.symbol, gtt.side, gtt.qty, gtt.trigger_price, gtt.tag) == (
        "112127",
        "INFY",
        Side.SELL,
        1,
        Decimal("702"),
        None,
    )
    assert parse_gtt_row({**single, "status": "triggered"}, same) is not None
    assert parse_gtt_row(two_leg, same) is None
    assert parse_gtt_row(cancelled, same) is None
    with pytest.raises(ValueError):
        parse_gtt_row({**single, "status": "paused"}, same)


def test_place_and_ack_results() -> None:
    assert place_result(KiteReply(OK, "151220000000000")) == ("placed", "151220000000000", CODE_OK)
    assert place_result(KiteReply(OK, FIXTURE["gtt_place"]), "trigger_id") == (
        "placed",
        "123",
        CODE_OK,
    )
    assert place_result(KiteReply(OK, {}), "trigger_id") == ("unconfirmed", None, CODE_UNCONFIRMED)
    assert place_result(KiteReply(UNKNOWN)) == ("unconfirmed", None, CODE_UNCONFIRMED)
    assert place_result(KiteReply(REFUSED)) == ("rejected", None, CODE_REJECTED)
    assert place_result(KiteReply(EXPIRED)) == ("rejected", None, CODE_SESSION_EXPIRED)
    assert place_result(KiteReply(NOT_SENT)) == ("rejected", None, CODE_UNAVAILABLE)
    assert ack_result(KiteReply(OK)).ok
    assert ack_result(KiteReply(REFUSED)).code == CODE_REJECTED
    assert ack_result(KiteReply(UNKNOWN)).code == CODE_UNAVAILABLE


# --- errors, login, token expiry, setting -------------------------------------------------------


class KiteException(Exception):  # noqa: N818 - the SDK's hierarchy, by name (`kiteconnect.exceptions`)
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.code = code


class TokenException(KiteException): ...


class InputException(KiteException): ...


class OrderException(KiteException): ...


class NetworkException(KiteException): ...


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (TokenException("Incorrect `api_key` or `access_token`.", 403), EXPIRED),
        (InputException("Invalid `quantity`.", 400), REFUSED),
        (OrderException("Insufficient funds.", 400), REFUSED),
        (NetworkException("Too many requests", 429), RATE_LIMITED),
        (NetworkException("Gateway timed out", 504), UNKNOWN),
    ],
)
def test_kite_failure(error: Exception, outcome: str) -> None:
    reply = kite_failure(error)
    assert reply is not None
    assert reply.outcome == outcome
    assert reply.error == type(error).__name__  # the class name, never Kite's text
    assert kite_failure(TimeoutError()) is None  # no answer: the guard's to count


def test_session_reply_fields() -> None:
    assert session_reply_fields(FIXTURE["session"]) == ("yyyyyy", "XX0000")
    for bad in (None, [], {"access_token": "", "user_id": "XX0000"}, {"access_token": "y"}):
        with pytest.raises(TokenRejectedError):
            session_reply_fields(bad)


@pytest.mark.parametrize(
    ("now", "expires"),
    [
        (datetime(2026, 10, 23, 8, 30, tzinfo=IST), datetime(2026, 10, 24, 6, 0, tzinfo=IST)),
        (datetime(2026, 10, 24, 5, 59, tzinfo=IST), datetime(2026, 10, 24, 6, 0, tzinfo=IST)),
        (datetime(2026, 10, 24, 6, 0, tzinfo=IST), datetime(2026, 10, 25, 6, 0, tzinfo=IST)),
    ],
)
def test_kite_token_expiry(now: datetime, expires: datetime) -> None:
    """A Kite token lasts until the next 06:00 IST."""
    assert kite_token_expiry(now) == expires
    assert kite_token_expiry(now).astimezone(IST).time() == time(6)
    assert kite_token_expiry(now) - now <= timedelta(days=1)
    naive = now.replace(tzinfo=None)
    with pytest.raises(ValueError):
        kite_token_expiry(naive)


def test_active_broker_setting() -> None:
    assert active_broker({}) == "fyers"
    assert active_broker({"HAT_BROKER": " Zerodha "}) == "zerodha"
    with pytest.raises(HatConfigError):
        active_broker({"HAT_BROKER": "upstox"})


# --- symbols ------------------------------------------------------------------------------------


def _udiff_zip(rows: list[dict[str, str]]) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=["TckrSymb", "SctySrs", "ISIN", "FinInstrmId"])
    writer.writeheader()
    writer.writerows(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv", text.getvalue())
    return buffer.getvalue()


def test_kite_symbol_mapping() -> None:
    """An instrument maps (ISIN, series) -> NSE token -> the Kite row with that token, only when
    the row names the instrument's symbol; indices, BSE and derivatives rows are not listings."""
    tokens = nse_tokens(
        _udiff_zip(
            [
                {
                    "TckrSymb": "INFY",
                    "SctySrs": "EQ",
                    "ISIN": "INE009A01021",
                    "FinInstrmId": "1594",
                },
                {
                    "TckrSymb": "SBIN",
                    "SctySrs": "EQ",
                    "ISIN": "INE062A01020",
                    "FinInstrmId": "3045",
                },
                {"TckrSymb": "ACC", "SctySrs": "BE", "ISIN": "INE012A01025", "FinInstrmId": "22"},
            ]
        )
    )
    assert tokens == {
        ("INE009A01021", "EQ"): 1594,
        ("INE062A01020", "EQ"): 3045,
        ("INE012A01025", "BE"): 22,
    }
    listings = kite_listings(dump_rows(FIXTURE["instruments_csv"]))
    assert listings == {
        1594: KiteListing("INFY", 408065),
        3045: KiteListing("SBIN", 779521),
        22: KiteListing("ACC", 5633),
    }
    assert kite_symbol("INE009A01021", "EQ", "INFY", tokens, listings) == KiteListing(
        "INFY", 408065
    )
    assert kite_symbol("INE062A01020", "EQ", "SBI", tokens, listings) is None  # symbol mismatch
    assert kite_symbol("INE012A01025", "BE", "ACC", tokens, listings) is None  # expects ACC-BE
    assert kite_symbol("INE009A01021", "SM", "INFY", tokens, listings) is None  # not routed
    clash = [*dump_rows(FIXTURE["instruments_csv"]), {**dump_rows(FIXTURE["instruments_csv"])[0]}]
    clash[-1]["instrument_token"] = "1"
    assert 1594 not in kite_listings(clash)  # one token, two rows: names no one instrument
    assert kite_db_symbol("ACC-BE") == "ACC"
    assert kite_db_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO"


@pytest.mark.parametrize(
    ("stored", "mapped", "replaces"),
    [
        (0, 3, True),  # the first refresh
        (0, 0, False),  # never replaced with nothing
        (2543, 0, False),
        (2543, 2540, True),
        (2543, 2035, True),  # 80% kept
        (2543, 2034, False),  # a drop that large is a bad input: the last set stays
        (2543, 3, False),
    ],
)
def test_plausible_refresh(stored: int, mapped: int, replaces: bool) -> None:
    assert plausible_refresh(stored, mapped) is replaces


def _unreadable() -> list[tuple[str, str]]:
    raise OSError("database down")


class _NoCalls:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"Kite called: {name}")


def test_symbol_lookup_fails_closed() -> None:
    """An order or GTT for an instrument whose Kite symbol cannot be read, or is not stored,
    is refused before any Kite call, never sent under the database symbol; a read fails."""
    gtt = GttRequest("ACC", 1, Decimal("100"), Decimal("99"), tag=GTT_TAG)
    down = KiteHatBroker(broker=_NoCalls(), instruments=_unreadable)  # type: ignore[arg-type]
    assert down.place_order(_intent(OrderType.MARKET, symbol="ACC")).code == CODE_UNAVAILABLE
    assert down.place_gtt_stop(gtt).code == CODE_UNAVAILABLE
    with pytest.raises(BrokerUnavailableError):
        down.orders()
    unmapped = KiteHatBroker(broker=_NoCalls(), instruments=lambda: [("INFY", "INFY")])  # type: ignore[arg-type]
    intent = _intent(OrderType.MARKET, symbol="ACC")
    with pytest.raises(InvalidIntentError):
        unmapped.place_order(intent)
    with pytest.raises(InvalidIntentError):
        unmapped.place_gtt_stop(gtt)


def test_stop_is_below() -> None:
    """A sell stop's trigger must sit strictly below the last price Kite places it with."""
    assert stop_is_below(Decimal("99.95"), Decimal("100"))
    assert not stop_is_below(Decimal("100"), Decimal("100"))
    assert not stop_is_below(Decimal("100.05"), Decimal("100"))
