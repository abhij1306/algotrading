"""The Kite (Zerodha) adapter behind `HatBroker`, as `fyers_adapter` is for Fyers (HAT-BRK-080,
-081). Engine, risk and router code never see it: they see `broker.HatBroker`, built for the one
active broker (`settings.broker`, `accounts.active_adapters`).

It wraps `KiteBroker` (the transport: the shared guard, Kite's breaker and budget), `KiteSession`
and `KiteAuthService`, and never touches the SDK, a token file or an environment variable itself.
Every payload and every reply goes through the pure builders and parsers of `kite_payloads.py`: an
invalid intent raises `InvalidIntentError` before any broker call, and a reply that cannot be read
fails the read. Reads fail closed (`BrokerUnavailableError`, never an empty list); results carry
fixed codes, never Kite's text.

Trading symbols come from `hat_broker_symbol` (broker zerodha, `kite_instruments`), both ways: an
order goes out under the stored symbol (the database symbol itself without one: an EQ stock's
Kite symbol), and a book row's symbol maps back through the same rows.

Kite places a GTT against the instrument's last price, which sets its direction, and replaces the
whole GTT on a modify; so a GTT place reads the last price first (`ltp`), a modify also reads the
GTT for its symbol, and a sell stop whose trigger is not below the last price is refused without
being sent (Kite would read it as a trigger on a rise). A failed read before a GTT write sends
nothing: the result is a rejection with the read's code, never `unconfirmed`.

GTT for delivery is not yet verified on a live Kite account, so `KITE_CAPABILITIES` reports none:
an account on Kite stays shadow until the owner's live check sets it (trading-safety rule 2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from app.hat.data.bhavcopy import Fetcher, http_fetcher
from app.hat.data.kite_symbols import (
    KiteSymbolData,
    bhavcopy_archive,
    dump_rows,
    kite_listings,
    latest_nse_tokens,
)
from app.hat.data.store import DataStore

from .broker import (
    CODE_INVALID_INTENT,
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
    BrokerUnavailableError,
    Capabilities,
    GttModify,
    GttRequest,
    GttResult,
    InvalidIntentError,
    OrderBook,
    OrderIntent,
    OrderType,
    PlaceResult,
    SessionStatus,
    TradeBook,
)
from .fyers_adapter import CODE_INVALID_RESPONSE, TOKEN_CHECK_SECONDS, map_session
from .kite_auth import KiteAuthService, get_kite_auth, kite_credentials_from_env
from .kite_broker import (
    KITE_BREAKER,
    KiteBroker,
    KiteRateLimitedError,
    KiteSessionExpiredError,
    KiteUnavailableError,
    kite_budget,
)
from .kite_payloads import (
    UNREADABLE,
    ack_result,
    check_gtt_leg,
    check_gtt_request,
    gtt_modify_params,
    gtt_params,
    kite_db_symbol,
    ltp_key,
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
from .kite_session import KiteSession, get_kite_session, kite_model

__all__ = [
    "KITE_CAPABILITIES",
    "KiteHatBroker",
    "build_kite_broker",
    "kite_instrument_dump",
    "kite_symbol_source",
]

logger = logging.getLogger(__name__)

# Not verified on a live Kite account yet: no real order goes out through Kite until the owner's
# live GTT check (place, read, modify, cancel a single-leg CNC sell GTT) sets this to True.
KITE_CAPABILITIES = Capabilities(gtt_delivery=False)


def _stored_instruments() -> list[tuple[str, str]]:
    """(database symbol, Kite trading symbol) of the active instruments with a zerodha mapping."""
    from app.database import HatSessionLocal  # deferred: importing it connects the engine
    from app.hat.data.broker_symbols import kite_instruments

    with HatSessionLocal() as session:
        return kite_instruments(session)


def _read_code(error: Exception) -> str:
    """The fixed code of a failed Kite read."""
    if isinstance(error, KiteSessionExpiredError):
        return CODE_SESSION_EXPIRED
    if isinstance(error, KiteRateLimitedError):
        return CODE_RATE_LIMITED
    return CODE_UNAVAILABLE


class KiteHatBroker:
    """`HatBroker` over the Kite stack in this package. Constructor arguments exist so tests
    inject stand-ins (never a real SDK); production passes none and shares Kite's breaker and
    budget across every adapter in the process."""

    def __init__(
        self,
        broker: KiteBroker | None = None,
        *,
        client: Callable[[], KiteSession] = get_kite_session,
        auth: Callable[[], KiteAuthService] = get_kite_auth,
        instruments: Callable[[], Iterable[tuple[str, str]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        capabilities: Capabilities = KITE_CAPABILITIES,
    ) -> None:
        self._broker = (
            broker if broker is not None else KiteBroker(breaker=KITE_BREAKER, budget=kite_budget())
        )
        self._capabilities = capabilities
        self._client = client
        self._auth = auth
        self._instruments = instruments if instruments is not None else _stored_instruments
        self._clock = clock
        self._token_check: tuple[object, bool, float] | None = None
        self._token_check_lock = threading.Lock()

    # --- capabilities and session ---------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def day_budget_spent(self) -> bool:
        budget = self._broker.budget
        return budget is not None and budget.day_spent()

    def unreachable_for(self) -> float:
        return self._broker.unreachable_for()

    def write_refusal(self) -> str | None:
        return self._broker.write_refusal()

    def session_status(self) -> SessionStatus:
        try:
            auth = self._auth()
            if auth.token_refused():
                return SessionStatus.EXPIRED  # until a new login (C22 A5)
            connected = auth.status().connected
            model = self._client().kite
        except Exception:
            logger.exception("Kite session status unreadable")
            return map_session(connected=False, has_client=False, store_error=True)
        status = map_session(connected=connected, has_client=model is not None, store_error=False)
        if status is SessionStatus.LIVE and self._token_refused(model):
            return SessionStatus.EXPIRED
        return status

    def _token_refused(self, model: object) -> bool:
        """Whether Kite itself refuses the saved token (`TokenException` on a profile read), at
        most one read per client per TOKEN_CHECK_SECONDS; any other failure leaves the status as
        the token file says (every read still fails closed)."""
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
            except KiteSessionExpiredError:
                logger.warning("Kite refused the saved token; log in again")
                refused = True
                auth.refuse_token(generation)  # the login status reads not connected (C22 A5)
            except Exception:
                logger.debug("Kite session check unavailable", exc_info=True)
            self._token_check = (model, refused, now)
            return refused

    # --- symbols --------------------------------------------------------------------------

    def _stored(self) -> dict[str, str]:
        """The stored zerodha mappings; `BrokerUnavailableError` when they cannot be read (an
        empty map would send a trade-for-trade stock under its EQ name)."""
        try:
            return dict(self._instruments())
        except Exception:
            logger.warning("Stored Kite symbols unreadable")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None

    def _tradingsymbol(self, symbol: str) -> str:
        """The stored Kite trading symbol orders for `symbol` go out under (`-BE` for a
        trade-for-trade stock). An instrument without one is refused (`InvalidIntentError`):
        its Kite row was missing or named another symbol, so no name is guessed."""
        stored = self._stored().get(symbol)
        if not stored:
            logger.warning("No Kite trading symbol stored for %s: not sent", symbol)
            raise InvalidIntentError(CODE_INVALID_INTENT)
        return stored

    def _to_db(self) -> Callable[[str], str]:
        """Kite trading symbol -> database symbol, by the stored rows, else `kite_db_symbol` (a
        stock HAT never mapped); a failed lookup fails the read (`BrokerUnavailableError`)."""
        back = {kite: db for db, kite in self._stored().items()}
        return lambda tradingsymbol: back.get(tradingsymbol) or kite_db_symbol(tradingsymbol)

    def _last_price(self, tradingsymbol: str) -> Decimal:
        key = ltp_key(tradingsymbol)
        return self._read(lambda: self._broker.fetch_ltp(key), lambda reply: parse_ltp(reply, key))

    # --- writes ---------------------------------------------------------------------------

    def place_order(self, intent: OrderIntent) -> PlaceResult:
        try:
            params = order_params(intent, self._tradingsymbol)
        except BrokerUnavailableError as error:
            return PlaceResult("rejected", None, error.code)  # the lookup failed: nothing sent
        try:
            reply = self._broker.place_order(params)
        except Exception:
            # The order may have reached the broker: never read this as a rejection.
            logger.exception("Kite place_order raised")
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
        params = order_modify_params(order_type, qty, price, trigger_price)
        if params is None:
            return AckResult(ok=False, code=CODE_REJECTED)  # a price the type needs is missing
        return self._write(lambda: self._broker.modify_order(broker_order_id, params))

    def cancel_order(self, broker_order_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_order(broker_order_id))

    @staticmethod
    def _write(call: Callable[[], Any]) -> AckResult:
        try:
            return ack_result(call())
        except Exception:
            logger.exception("Kite write failed")
            return AckResult(ok=False, code=CODE_UNAVAILABLE)

    def place_gtt_stop(self, request: GttRequest) -> GttResult:
        trigger = check_gtt_request(request)  # before any call
        try:
            tradingsymbol = self._tradingsymbol(request.symbol)
            last = self._last_price(tradingsymbol)
        except BrokerUnavailableError as error:
            return GttResult("rejected", None, error.code)  # nothing was sent
        if not stop_is_below(trigger, last):
            logger.warning(
                "Kite GTT for %s not sent: its trigger is not below the last price", request.symbol
            )
            return GttResult("rejected", None, CODE_REJECTED)
        params = gtt_params(request, lambda _symbol: tradingsymbol, last)
        try:
            reply = self._broker.place_gtt(params)
        except Exception:
            logger.exception("Kite place_gtt raised")
            return GttResult("unconfirmed", None, CODE_UNCONFIRMED)
        status, gtt_id, code = place_result(reply, "trigger_id")
        return GttResult(status, gtt_id, code)

    def modify_gtt(self, request: GttModify) -> AckResult:
        trigger = check_gtt_leg(request.qty, request.trigger_price, request.limit_price)
        try:
            row = self._read(
                lambda: self._broker.fetch_gtt(request.gtt_id),
                lambda raw: str(raw["condition"]["tradingsymbol"]),
            )
            last = self._last_price(row)
        except BrokerUnavailableError as error:
            return AckResult(ok=False, code=error.code)  # nothing was sent
        if not stop_is_below(trigger, last):
            logger.warning(
                "Kite GTT %s not modified: its trigger is not below the last price", request.gtt_id
            )
            return AckResult(ok=False, code=CODE_REJECTED)
        params = gtt_modify_params(
            row, request.qty, request.trigger_price, request.limit_price, last
        )
        return self._write(lambda: self._broker.modify_gtt(request.gtt_id, params))

    def cancel_gtt(self, gtt_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_gtt(gtt_id))

    def gtts(self) -> list[BrokerGtt]:
        """HAT-shaped GTTs at the broker (single-leg CNC, resting or triggered)."""
        to_db = self._to_db()
        return self._read(
            self._broker.fetch_gtts,
            lambda rows: [g for g in (parse_gtt_row(r, to_db) for r in rows_of(rows)) if g],
        )

    # --- reads (fail closed) --------------------------------------------------------------

    def _read(self, fetch: Callable[[], Any], parse: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Fetch and parse; any failure is a fixed-code BrokerUnavailableError
        (`broker_session_expired` when Kite refused the token, which also ends the standing
        profile check; `broker_rate_limited` for a rate limit)."""
        try:
            raw = fetch()
        except KiteSessionExpiredError:
            logger.warning("Kite refused the token on a read")
            with self._token_check_lock:
                self._token_check = None
            raise BrokerUnavailableError(CODE_SESSION_EXPIRED) from None
        except KiteUnavailableError as error:
            logger.warning("Kite read unavailable (%s)", type(error).__name__)
            raise BrokerUnavailableError(_read_code(error)) from None
        except Exception:
            logger.exception("Kite read failed")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None
        try:
            return parse(raw)
        except UNREADABLE:
            logger.exception("Kite reply could not be read")
            raise BrokerUnavailableError(CODE_INVALID_RESPONSE) from None

    def orders(self) -> OrderBook:
        to_db = self._to_db()
        return self._read(self._broker.fetch_orders, lambda rows: parse_order_book(rows, to_db))

    def trades(self) -> TradeBook:
        """The day's trades with their orders' tags: the trades are read first, so every order
        they name is in the order book read after them."""
        to_db = self._to_db()
        rows = self._read(self._broker.fetch_trades, rows_of)
        return self._read(
            self._broker.fetch_orders, lambda orders: parse_trades(rows, orders, to_db)
        )

    def positions(self) -> list[BrokerPosition]:
        to_db = self._to_db()
        return self._read(self._broker.fetch_positions, lambda reply: parse_positions(reply, to_db))

    def holdings(self) -> list[BrokerHolding]:
        to_db = self._to_db()
        return self._read(self._broker.fetch_holdings, lambda rows: parse_holdings(rows, to_db))

    def funds(self) -> BrokerFunds:
        return self._read(self._broker.fetch_margins, parse_funds)


def build_kite_broker() -> KiteHatBroker:
    """The production adapter: the process-wide session, auth service, breaker and budget."""
    return KiteHatBroker()


def kite_instrument_dump() -> list[Any]:
    """Kite's NSE instrument dump (the SDK's `instruments("NSE")` rows), read with the logged-in
    client, else with the app's key alone (the dump needs no session). Called by the weekly
    symbol refresh and the admin command only, never on the order path."""
    model = get_kite_session().kite
    if model is None:
        model = kite_model(api_key=kite_credentials_from_env().api_key)
    return list(model.instruments("NSE"))


def kite_symbol_source(
    store: DataStore, fetch: Fetcher | None = None
) -> Callable[[date, str | None], KiteSymbolData]:
    """What the zerodha symbol refresh maps by, as of a date: the NSE tokens of the latest
    bhavcopy (kept in the store's `raw/`, else downloaded with `fetch`, default the NSE fetcher)
    and Kite's NSE listings, from a dump CSV's text when one is given, else Kite's download."""
    archive = bhavcopy_archive(store, fetch if fetch is not None else http_fetcher())

    def read(day: date, dump: str | None = None) -> KiteSymbolData:
        rows = dump_rows(dump) if dump is not None else kite_instrument_dump()
        return latest_nse_tokens(archive, day), kite_listings(rows)

    return read
