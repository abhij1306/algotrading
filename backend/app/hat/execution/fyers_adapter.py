"""The Fyers adapter behind `HatBroker` (HAT-DEC-BD2, HAT-BRK-081, -090, -091).

It wraps `FyersBroker` (the transport: the circuit breaker, the guarded timeout, the call budget
and `SUBMIT_UNCONFIRMED`), `FyersSession` (is the SDK model usable) and `FyersAuthService`
(login link and code exchange), all in this package (K2). It never touches the SDK, a token file
or an environment variable itself (HAT-OPS-051). The instrument list comes from `hat_instrument`
and `hat_broker_symbol`.

Every payload it sends and every reply it reads goes through the pure builders and parsers of
`fyers_payloads.py` (architecture audit F8, X7): an invalid intent raises
`InvalidIntentError` before any broker call, and a reply that cannot be read fails the read.
Reads fail closed: when the broker cannot answer they raise `BrokerUnavailableError`, never an
empty list. Results carry fixed codes, not broker text.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from app.hat.data.symbols import symbol_master

from .broker import (
    CODE_RATE_LIMITED,
    CODE_REJECTED,
    CODE_SESSION_EXPIRED,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    AckResult,
    BrokerFunds,
    BrokerGtt,
    BrokerHolding,
    BrokerPosition,
    BrokerTrade,
    BrokerUnavailableError,
    Capabilities,
    GttModify,
    GttRequest,
    GttResult,
    OrderBook,
    OrderIntent,
    OrderType,
    PlaceResult,
    SessionStatus,
    write_refusal,
)
from .fyers_auth import (
    FyersAuthService,
    get_fyers_auth,
)
from .fyers_broker import (
    PROCESS_BREAKER,
    FyersBroker,
    FyersRateLimitedError,
    FyersSessionExpiredError,
    FyersUnavailableError,
    process_budget,
)
from .fyers_payloads import (
    UNREADABLE,
    ack_result,
    gtt_modify_fields,
    gtt_payload,
    order_modify_fields,
    order_payload,
    parse_funds,
    parse_gtt_row,
    parse_holdings,
    parse_order_book,
    parse_positions,
    parse_trade,
    place_result,
)
from .fyers_session import FyersSession, get_fyers_session

__all__ = [
    "CODE_INVALID_RESPONSE",
    "FYERS_CAPABILITIES",
    "FyersHatBroker",
    "build_fyers_broker",
    "map_session",
]

logger = logging.getLogger(__name__)

CODE_INVALID_RESPONSE = "broker_response_invalid"

# Fyers documents a 1-20 alphanumeric orderTag. GTT for delivery was verified on the live account
# on 2026-10-22 (HAT-BRK-091): a single-leg CNC sell GTT was placed (with and without a
# tag), read in the GTT book, modified and cancelled (`tests/hat/fixtures/fyers_live_writes.json`).
FYERS_CAPABILITIES = Capabilities(gtt_delivery=True)

TOKEN_CHECK_SECONDS = 30.0  # how long one profile check of a token stands (as the login status)


def map_session(*, connected: bool, has_client: bool, store_error: bool) -> SessionStatus:
    """The session status from the token store and the SDK client. Only LIVE can place orders."""
    if store_error:
        return SessionStatus.UNKNOWN
    if not connected:
        return SessionStatus.MISSING
    return SessionStatus.LIVE if has_client else SessionStatus.EXPIRED


def _stored_instruments() -> list[tuple[str, str]]:
    """(database symbol, Fyers symbol) of the active instruments with a Fyers mapping, read from
    `hat_instrument` and `hat_broker_symbol`."""
    from app.database import HatSessionLocal  # deferred: importing it connects the engine
    from app.hat.data.instruments import fyers_instruments

    with HatSessionLocal() as session:
        return fyers_instruments(session)


class FyersHatBroker:
    """`HatBroker` over the Fyers stack in this package. Constructor arguments exist so tests
    inject `FakeBroker` (never a real SDK); production passes none and shares one circuit
    breaker across every adapter in the process."""

    def __init__(
        self,
        broker: FyersBroker | None = None,
        *,
        client: Callable[[], FyersSession] = get_fyers_session,
        auth: Callable[[], FyersAuthService] = get_fyers_auth,
        instruments: Callable[[], Iterable[tuple[str, str]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        capabilities: Capabilities = FYERS_CAPABILITIES,
    ) -> None:
        self._broker = (
            broker
            if broker is not None
            else FyersBroker(breaker=PROCESS_BREAKER, budget=process_budget())
        )
        self._capabilities = capabilities  # FYERS_CAPABILITIES in production; rigs set others
        self._client = client
        self._auth = auth
        self._instruments = instruments if instruments is not None else _stored_instruments
        self._clock = clock
        # the last profile check: (SDK model it checked, refused, when); see `_token_refused`
        self._token_check: tuple[object, bool, float] | None = None
        self._token_check_lock = threading.Lock()

    # --- capabilities and session ---------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def day_budget_spent(self) -> bool:
        budget = self._broker.budget
        return budget is not None and budget.day_spent()

    def write_refusal(self) -> str | None:
        budget = self._broker.budget
        spent = budget is not None and budget.writes_spent()
        return write_refusal(self._broker.breaker.is_open(), spent)

    def session_status(self) -> SessionStatus:
        try:
            auth = self._auth()
            if auth.token_refused():
                return SessionStatus.EXPIRED  # until a new login (C22 A5)
            connected = auth.status().connected
            model = self._client().fyers
            has_client = model is not None
        except Exception:
            logger.exception("Fyers session status unreadable")
            return map_session(connected=False, has_client=False, store_error=True)
        status = map_session(connected=connected, has_client=has_client, store_error=False)
        if status is SessionStatus.LIVE and self._token_refused(model):
            return SessionStatus.EXPIRED
        return status

    def _token_refused(self, model: object) -> bool:
        """Whether Fyers itself refuses the saved token (code -16 on a profile read): a token
        revoked or expired at the broker reads expired though the token file has not expired.
        Any other failure of the check is an outage, not a lost session: the status stays as the
        token file says, and every read still fails closed. One profile read per SDK model (a new
        token builds a new one; the adapter never reads the token) per TOKEN_CHECK_SECONDS, so
        the session watch and the entry checks add no load, or breaker failures, of their own."""
        with self._token_check_lock:
            now = self._clock()
            check = self._token_check
            if check is not None and check[0] is model and now - check[2] < TOKEN_CHECK_SECONDS:
                return check[1]
            refused = False
            try:
                auth = self._auth()
                generation = auth.token_generation()
                self._broker.get_profile()
            except FyersSessionExpiredError:
                logger.warning("Fyers refused the saved token; log in again")
                refused = True
                auth.refuse_token(generation)  # the login status reads not connected (C22 A5)
            except Exception:
                logger.debug("Fyers session check unavailable", exc_info=True)
            self._token_check = (model, refused, now)
            return refused

    # --- writes ---------------------------------------------------------------------------

    def _fyers_symbol(self, symbol: str) -> str:
        """The Fyers symbol orders for `symbol` go out under: the stored broker symbol (`-BE` for a
        trade-for-trade stock), else the converted `-EQ` one."""
        try:
            stored = dict(self._instruments()).get(symbol_master.to_db(symbol))
        except Exception:
            logger.warning("Stored Fyers symbols unreadable; converting %s", symbol)
            stored = None
        return stored or symbol_master.to_fyers(symbol)

    def place_order(self, intent: OrderIntent) -> PlaceResult:
        order = order_payload(intent, self._fyers_symbol)
        try:
            reply = self._broker.place_order(order)
        except Exception:
            # The order may have reached the broker: never read this as a rejection.
            logger.exception("Fyers place_order raised")
            return PlaceResult("unconfirmed", None, CODE_UNCONFIRMED)
        status, order_id, code = place_result(reply)
        return PlaceResult(status, order_id, code)

    def modify_order(
        self,
        broker_order_id: str,
        *,
        order_type: OrderType,
        qty: int | None = None,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> AckResult:
        fields = order_modify_fields(order_type, qty, price, trigger_price)
        if fields is None:
            return AckResult(ok=False, code=CODE_REJECTED)  # a price the type needs is missing
        return self._write(lambda: self._broker.modify_order(broker_order_id, fields))

    def cancel_order(self, broker_order_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_order(broker_order_id))

    @staticmethod
    def _write(call: Callable[[], object]) -> AckResult:
        try:
            return ack_result(call())
        except Exception:
            logger.exception("Fyers write failed")
            return AckResult(ok=False, code=CODE_UNAVAILABLE)

    def place_gtt_stop(self, request: GttRequest) -> GttResult:
        gtt = gtt_payload(request, self._fyers_symbol)
        try:
            reply = self._broker.place_gtt(gtt)
        except Exception:
            logger.exception("Fyers place_gtt raised")
            return GttResult("unconfirmed", None, CODE_UNCONFIRMED)
        status, gtt_id, code = place_result(reply)
        return GttResult(status, gtt_id, code)

    def modify_gtt(self, request: GttModify) -> AckResult:
        fields = gtt_modify_fields(request)
        return self._write(lambda: self._broker.modify_gtt(request.gtt_id, fields))

    def cancel_gtt(self, gtt_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_gtt(gtt_id))

    def gtts(self) -> list[BrokerGtt]:
        """HAT-shaped GTTs at the broker (single-leg CNC, resting or triggered)."""
        return self._read(
            self._broker.fetch_gtt_orders,
            lambda rows: [gtt for gtt in map(parse_gtt_row, rows) if gtt is not None],
        )

    # --- reads (fail closed) --------------------------------------------------------------

    def _read(self, fetch: Callable[[], Any], parse: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Fetch and parse; any failure is a fixed-code BrokerUnavailableError
        (`broker_session_expired` when Fyers refused the token, which also ends the standing
        profile check, so the next `session_status` reads the refusal at once;
        `broker_rate_limited` when the plan's daily requests are spent)."""
        try:
            raw = fetch()
        except FyersSessionExpiredError:
            logger.warning("Fyers refused the token on a read")
            with self._token_check_lock:
                self._token_check = None
            raise BrokerUnavailableError(CODE_SESSION_EXPIRED) from None
        except FyersRateLimitedError:
            logger.warning("Fyers daily request limit reached on a read")
            raise BrokerUnavailableError(CODE_RATE_LIMITED) from None
        except (FyersUnavailableError, BrokerUnavailableError):
            logger.warning("Fyers read unavailable")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None
        except Exception:
            logger.exception("Fyers read failed")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None
        try:
            return parse(raw)
        except UNREADABLE:
            logger.exception("Fyers reply could not be read")
            raise BrokerUnavailableError(CODE_INVALID_RESPONSE) from None

    def orders(self) -> OrderBook:
        return self._read(self._broker.fetch_orderbook, parse_order_book)

    def trades(self) -> list[BrokerTrade]:
        return self._read(self._broker.fetch_tradebook, lambda rows: [parse_trade(r) for r in rows])

    def positions(self) -> list[BrokerPosition]:
        return self._read(self._broker.fetch_positions, parse_positions)

    def holdings(self) -> list[BrokerHolding]:
        return self._read(self._broker.fetch_holdings, parse_holdings)

    def funds(self) -> BrokerFunds:
        return self._read(self._broker.fetch_funds, parse_funds)


def build_fyers_broker() -> FyersHatBroker:
    """The production adapter: the process-wide session, auth service and circuit breaker."""
    return FyersHatBroker()
