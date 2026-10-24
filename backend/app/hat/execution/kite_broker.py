"""The Kite (Zerodha) transport: the SDK calls HAT makes, through the same guard as Fyers'
(`fyers_broker.GuardedBroker`: the circuit breaker, the guarded timeout, the worker pool, the call
budget and the HTTP 429 watch), handing back what Kite answered.

Kite's SDK answers an error by raising (`kiteconnect.exceptions`: the HTTP status in `code`), not
with an error reply as Fyers does. `kite_failure` sorts what was raised, on the worker thread, into
one of the `KiteReply` outcomes, so a refusal is never read as a lost answer and an outage never as
a refusal:

- `refused`: Kite refused the call and did nothing (`InputException`, `OrderException`,
  `PermissionException`; `TokenException` is `expired`, the session's end);
- `rate_limited`: an HTTP 429, or a call the budget did not grant (nothing was sent);
- `not_sent`: no client (not logged in) or the circuit is open;
- `unknown`: no answer (timeout, transport error) or one that does not say what happened
  (`NetworkException`, Kite's OMS link failing; `GeneralException`; `DataException`): a place
  may have landed, so it is `unconfirmed` and resolved by its tag (trading-safety rule 3).

Reads (`fetch_*`) fail closed: anything but `ok` raises `KiteUnavailableError` (its subclasses
`KiteSessionExpiredError` and `KiteRateLimitedError` where it is one of those), never an empty
list. Kite has its own breaker and budget (`KITE_BREAKER`, `kite_budget`): its outage and limits
are not Fyers', whose market data keeps running. The budget's limits are Kite's published ones
(10 requests a second; orders 200 a minute) applied to every call; Kite has no daily request cap,
only an order cap far above HAT's use.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .fyers_broker import (
    WRITE,
    CallBudget,
    CallLimits,
    CircuitBreaker,
    GuardedBroker,
    rate_limited,
    watch_responses,
)
from .kite_session import get_kite_session

__all__ = [
    "EXPIRED",
    "KITE_BREAKER",
    "KITE_LIMITS",
    "NOT_SENT",
    "OK",
    "RATE_LIMITED",
    "REFUSED",
    "UNKNOWN",
    "KiteBroker",
    "KiteRateLimitedError",
    "KiteReply",
    "KiteSessionExpiredError",
    "KiteUnavailableError",
    "kite_budget",
    "kite_failure",
]

logger = logging.getLogger(__name__)

OK = "ok"
REFUSED = "refused"
EXPIRED = "expired"
RATE_LIMITED = "rate_limited"
NOT_SENT = "not_sent"
UNKNOWN = "unknown"

HTTP_TOO_MANY_REQUESTS = 429
# Kite's exceptions that answer a call Kite refused without acting on it
_REFUSALS = frozenset({"InputException", "OrderException", "PermissionException"})
_SESSION_REFUSAL = "TokenException"
_KITE_BASE = "KiteException"

KITE_LIMITS = CallLimits(per_second=10, per_minute=200, per_day=None)
KITE_BREAKER = CircuitBreaker()
"""The breaker the production Kite transports share: one Kite app, one outage."""

WRITE_TIMEOUT = 1.5
READ_TIMEOUT = 2.0


@dataclass(frozen=True)
class KiteReply:
    """What one Kite SDK call came to: `outcome` (one of the module's outcome words) and, for
    `ok`, what the SDK returned; for a failure, the Kite exception's class name (never its text)."""

    outcome: str
    data: Any = None
    error: str | None = None


class KiteUnavailableError(Exception):
    """The Kite client is missing or a read failed (an outage, never an empty book)."""


class KiteSessionExpiredError(KiteUnavailableError):
    """Kite refused the token (`TokenException`): only a new login brings it back."""


class KiteRateLimitedError(KiteUnavailableError):
    """Kite refused the call for its rate limit (HTTP 429), or HAT's budget did not grant it."""


def kite_failure(error: BaseException) -> KiteReply | None:
    """The reply a Kite SDK exception stands for; None for anything else (a transport error or
    a timeout: no answer, which the guard counts toward opening the circuit). Told by the class
    name, as the SDK is imported only by `kite_ipv4`."""
    names = {cls.__name__ for cls in type(error).__mro__}
    if _KITE_BASE not in names:
        return None
    name = type(error).__name__
    if getattr(error, "code", None) == HTTP_TOO_MANY_REQUESTS:
        return KiteReply(RATE_LIMITED, error=name)
    if name == _SESSION_REFUSAL:
        return KiteReply(EXPIRED, error=name)
    if name in _REFUSALS:
        return KiteReply(REFUSED, error=name)
    return KiteReply(UNKNOWN, error=name)


_PROCESS_BUDGET: CallBudget | None = None
_BUDGET_LOCK = threading.Lock()


def kite_budget() -> CallBudget:
    """The budget every production Kite transport shares: one Kite app, one set of limits."""
    global _PROCESS_BUDGET
    with _BUDGET_LOCK:
        if _PROCESS_BUDGET is None:
            _PROCESS_BUDGET = CallBudget(KITE_LIMITS)
        return _PROCESS_BUDGET


_CIRCUIT = object()  # the guard's answer for a call the open circuit held back
_NO_ANSWER = object()  # its answer for a call that timed out or raised


class KiteBroker(GuardedBroker):
    """Order writes, GTT writes and fail-closed reads over a `KiteConnect` client.

    `client` returns the client (resolved on each use, so a re-login is picked up) or None when
    there is no usable session; the default asks the process-wide `KiteSession`. Each write takes
    the SDK's keyword arguments exactly as `kite_payloads` builds them.
    """

    label = "Kite"

    def __init__(
        self,
        client: Callable[[], Any] | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        budget: CallBudget | None = None,
    ) -> None:
        super().__init__(breaker=breaker, budget=budget)
        self._client_source = client if client is not None else lambda: get_kite_session().kite

    @property
    def client(self) -> Any:  # noqa: ANN401 - the untyped KiteConnect client
        """The current client, or None when there is no usable session."""
        try:
            model = self._client_source()
        except Exception as error:
            logger.warning("Kite broker not connected (%s)", type(error).__name__)
            return None
        if model is not None:
            watch_responses(model)  # every client a call resolves sees its 429s
        return model

    def _call(
        self, operation: str, call: Callable[[Any], object], *, kind: str, timeout: float
    ) -> KiteReply:
        """One guarded SDK call, sorted into a `KiteReply`."""
        model = self.client
        if model is None:
            return KiteReply(NOT_SENT, error="no_client")

        def run() -> object:
            try:
                return KiteReply(OK, call(model))
            except Exception as error:
                failure = kite_failure(error)
                if failure is None:
                    raise  # no answer: the guard counts it toward the circuit
                return failure

        result = self._call_with_guard(
            operation, run, timeout_sec=timeout, fallback=_NO_ANSWER, not_sent=_CIRCUIT, kind=kind
        )
        if result is _CIRCUIT:
            return KiteReply(NOT_SENT, error="circuit_open")
        if result is _NO_ANSWER:
            return KiteReply(UNKNOWN)
        if rate_limited(result):  # the budget's refusal or an HTTP 429 the guard saw
            return KiteReply(RATE_LIMITED)
        if isinstance(result, KiteReply):
            if result.outcome != OK:
                logger.warning("Kite %s answered %s (%s)", operation, result.outcome, result.error)
            return result
        return KiteReply(UNKNOWN)

    def _write(self, operation: str, call: Callable[[Any], object]) -> KiteReply:
        return self._call(operation, call, kind=WRITE, timeout=WRITE_TIMEOUT)

    def _read(self, operation: str, call: Callable[[Any], object]) -> Any:  # noqa: ANN401
        """What a read returned; raises when it was anything but `ok` (fail closed)."""
        reply = self._call(operation, call, kind="read", timeout=READ_TIMEOUT)
        if reply.outcome == OK:
            return reply.data
        if reply.outcome == EXPIRED:
            raise KiteSessionExpiredError(f"Kite {operation} session expired")
        if reply.outcome == RATE_LIMITED:
            raise KiteRateLimitedError(f"Kite {operation} request limit reached")
        raise KiteUnavailableError(f"Kite {operation} request failed")

    # --- writes ---------------------------------------------------------------------------

    def place_order(self, params: dict[str, Any]) -> KiteReply:
        """Send an order (`KiteConnect.place_order(**params)`); `ok` carries the order id."""
        return self._write("place_order", lambda kite: kite.place_order(**params))

    def modify_order(self, order_id: str, params: dict[str, Any]) -> KiteReply:
        return self._write(
            "modify_order",
            lambda kite: kite.modify_order(variety="regular", order_id=order_id, **params),
        )

    def cancel_order(self, order_id: str) -> KiteReply:
        return self._write(
            "cancel_order", lambda kite: kite.cancel_order(variety="regular", order_id=order_id)
        )

    def place_gtt(self, params: dict[str, Any]) -> KiteReply:
        """Send a GTT (`KiteConnect.place_gtt(**params)`); `ok` carries `{"trigger_id": ..}`."""
        return self._write("place_gtt", lambda kite: kite.place_gtt(**params))

    def modify_gtt(self, trigger_id: str, params: dict[str, Any]) -> KiteReply:
        return self._write(
            "modify_gtt", lambda kite: kite.modify_gtt(trigger_id=trigger_id, **params)
        )

    def cancel_gtt(self, trigger_id: str) -> KiteReply:
        return self._write("cancel_gtt", lambda kite: kite.delete_gtt(trigger_id))

    # --- reads (fail closed) --------------------------------------------------------------

    def get_profile(self) -> Any:  # noqa: ANN401
        return self._read("profile", lambda kite: kite.profile())

    def fetch_orders(self) -> Any:  # noqa: ANN401
        return self._read("orders", lambda kite: kite.orders())

    def fetch_trades(self) -> Any:  # noqa: ANN401
        return self._read("trades", lambda kite: kite.trades())

    def fetch_positions(self) -> Any:  # noqa: ANN401
        return self._read("positions", lambda kite: kite.positions())

    def fetch_holdings(self) -> Any:  # noqa: ANN401
        return self._read("holdings", lambda kite: kite.holdings())

    def fetch_margins(self) -> Any:  # noqa: ANN401
        return self._read("margins", lambda kite: kite.margins(segment="equity"))

    def fetch_gtts(self) -> Any:  # noqa: ANN401
        return self._read("gtts", lambda kite: kite.get_gtts())

    def fetch_gtt(self, trigger_id: str) -> Any:  # noqa: ANN401
        return self._read("gtt", lambda kite: kite.get_gtt(trigger_id))

    def fetch_ltp(self, key: str) -> Any:  # noqa: ANN401
        """`{key: {"last_price": ..}}` for one `EXCHANGE:SYMBOL` key."""
        return self._read("ltp", lambda kite: kite.ltp(key))
