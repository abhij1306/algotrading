"""The Fyers transport: the SDK calls HAT makes, with the circuit breaker, the guarded timeout
and the call budget (K2; cut down from the legacy `FyersBroker`, HAT-BRK-081, HAT-DEC-BD2).

It sends the payloads `fyers_adapter` builds as they are and hands back the raw replies: what an
order looks like and what a reply means are the adapter's (PR 75d, architecture audit F8).

Every SDK call goes through `_call_with_guard`: a worker-thread call with a timeout, and a circuit
breaker that opens after three failures in a row and stays open for 20 s. Nothing is sent while the
circuit is open, and a lost answer to a place is `SUBMIT_UNCONFIRMED`, never a rejection: the order
may have landed. Reads (`fetch_*`) fail closed: they raise `FyersUnavailableError`
when the broker cannot answer, never an empty list.

The breaker is an object, injected per broker, not class state (a failure recorded through one
subclass used to shadow another's). Production shares one (`PROCESS_BREAKER`) because every broker
instance talks to the same Fyers account.

The call budget (PR 50, HAT-BRK-082; research G18, O3) is one process-wide `CallBudget`
(`process_budget`), read from the settings (`HAT_BROKER_CALLS_PER_SECOND`, `_PER_MINUTE`,
`_PER_DAY`; defaults 8, 180 and the Standard plan's 5,000), so a plan with higher limits is a
configuration change only. Every SDK call takes one call from it first. Writes (orders, stops,
GTTs) go ahead of reads: a read may use only `READ_SHARE` of each window, waits while a write is
waiting, and waits at most `READ_WAIT`; a write waits at most `WRITE_WAIT`. A call the budget
cannot grant in time is never sent and is answered as Fyers' own rate limit (code 429), so a read
fails closed with `broker_rate_limited` (the fill sync tries again at its next step) and a write is
refused as not sent, burning no retry. An HTTP 429 from Fyers is the same refusal; its
`Retry-After` (else `THROTTLE_PAUSE`) pauses the whole budget.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import date
from threading import Lock
from typing import Any, TypedDict

from ..settings import load_settings
from ..time import trade_date_of, utc_now
from .fyers_session import get_fyers_session

__all__ = [
    "BROKER_CIRCUIT_OPEN",
    "BROKER_ORDER_UNCONFIRMED",
    "CLIENT_NOT_INITIALIZED",
    "CallBudget",
    "CallLimits",
    "PROCESS_BREAKER",
    "READ",
    "WRITE",
    "SUBMIT_UNCONFIRMED",
    "CircuitBreaker",
    "FyersBroker",
    "FyersRateLimitedError",
    "FyersSessionExpiredError",
    "FyersUnavailableError",
    "checked_reply",
    "process_budget",
    "rate_limited",
    "profile_data",
    "OrderReply",
]

logger = logging.getLogger(__name__)

CLIENT_NOT_INITIALIZED = "Client not initialized"
# OrderReply status when the order may have reached the broker but no reply confirmed it
# (timeout, transport error, malformed reply). It is neither placed nor rejected.
SUBMIT_UNCONFIRMED = "SUBMIT_UNCONFIRMED"
BROKER_ORDER_UNCONFIRMED = "Order sent but not confirmed by the broker"
BROKER_CIRCUIT_OPEN = "Broker unavailable; order not sent"


class OrderReply(TypedDict):
    order_id: str
    status: str
    message: str
    details: dict[str, Any] | None


class FyersUnavailableError(Exception):
    """The broker client is missing or a read call failed (an outage, never an empty book)."""


# Fyers answers a bad, expired or revoked token with code -16 ("Could not authenticate the user",
# "Token is Invalid or Expired"), as a normal reply, not an exception (recorded 2026-10-22).
FYERS_CODE_TOKEN_REJECTED = -16


class FyersSessionExpiredError(FyersUnavailableError):
    """Fyers refused the token (code -16): the session expired or was revoked, so only a new
    login brings it back. Still a failed read (an unreadable book, never an empty one)."""


# Fyers answers every call with code -360 ("Standard plan limit of 5,000 requests/day") once the
# app has spent its plan's daily requests, as a normal error reply: nothing succeeds until the
# next day.
FYERS_CODE_RATE_LIMITED = -360


class FyersRateLimitedError(FyersUnavailableError):
    """Fyers refused the call because a request limit is reached (code -360, the plan's daily
    requests are spent; or HTTP 429), or HAT's own call budget did not grant it (PR 50): not an
    outage. Still a failed read (an unreadable book, never an empty one)."""


# HTTP 429 (too many requests): Fyers' per-second or per-minute limit, or HAT's own call budget
# refusing to send (PR 50). Nothing was sent past the limit; it is refused like -360.
HTTP_TOO_MANY_REQUESTS = 429


def rate_limited(response: object) -> bool:
    """Whether `response` is a rate-limit refusal: Fyers' -360 (the plan's daily requests
    spent), or 429 (a per-second or per-minute limit, Fyers' or the call budget's)."""
    return (
        isinstance(response, dict)
        and response.get("s") != "ok"
        and response.get("code") in (FYERS_CODE_RATE_LIMITED, HTTP_TOO_MANY_REQUESTS)
    )


def _throttled(operation: str) -> dict[str, Any]:
    """The reply of a call the budget did not send, or that Fyers answered with HTTP 429."""
    return {"s": "error", "code": HTTP_TOO_MANY_REQUESTS, "message": f"{operation} rate limited"}


def checked_reply(operation: str, response: object) -> dict[str, Any]:
    """`response` when it is an ok Fyers reply; FyersSessionExpiredError for code -16,
    FyersRateLimitedError for code -360 or 429, FyersUnavailableError for anything else. The error
    text is fixed, never the broker's."""
    if not isinstance(response, dict) or response.get("s") != "ok":
        if isinstance(response, dict) and response.get("code") == FYERS_CODE_TOKEN_REJECTED:
            raise FyersSessionExpiredError(f"Fyers {operation} session expired")
        if rate_limited(response):
            raise FyersRateLimitedError(f"Fyers {operation} request limit reached")
        raise FyersUnavailableError(f"Fyers {operation} request failed")
    return response


def profile_data(response: object) -> dict[str, Any]:
    """The `data` of an ok profile reply (as `checked_reply` fails otherwise)."""
    data = checked_reply("profile", response).get("data")
    if not isinstance(data, dict):
        raise FyersUnavailableError("Fyers profile request failed")
    return data


def _rejected(message: str, details: dict[str, Any] | None = None) -> OrderReply:
    return OrderReply(order_id="", status="REJECTED", message=message, details=details)


def _unconfirmed() -> OrderReply:
    return OrderReply(
        order_id="", status=SUBMIT_UNCONFIRMED, message=BROKER_ORDER_UNCONFIRMED, details=None
    )


class CircuitBreaker:
    """Opens after `threshold` failures in a row and stays open for `recovery_sec`; one success
    closes it. Thread-safe."""

    def __init__(
        self,
        threshold: int = 3,
        recovery_sec: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.recovery_sec = recovery_sec
        self._clock = clock
        self._lock = Lock()
        self.failures = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() < self.open_until

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = self._clock() + self.recovery_sec


PROCESS_BREAKER = CircuitBreaker()
"""The breaker the production brokers share: one Fyers account, one outage."""

# --- the call budget (PR 50) ----------------------------------------------------------------

READ = "read"
WRITE = "write"
READ_SHARE = 0.8  # a read may use this share of each window: the rest is kept for writes
READ_WAIT = 0.5  # seconds a read waits for a call before failing as rate limited
WRITE_WAIT = 3.0  # seconds a write waits for a call before it is refused, not sent
THROTTLE_PAUSE = 1.0  # seconds the budget pauses after a 429 without a usable Retry-After
MAX_PAUSE = 60.0  # a Retry-After longer than this is read as this
_WRITER_POLL = 0.05  # how often a read waiting behind a write looks again
_WAIT_SLACK = 1e-6  # seconds added to a wait, against float rounding at the window edge


@dataclass(frozen=True)
class CallLimits:
    """Calls per second, per minute and per trading day (IST); `per_day` None is no day cap."""

    per_second: int = 8
    per_minute: int = 180
    per_day: int | None = 5000


def _read_limit(limit: int) -> int:
    """A read's share of a second or minute window: at least one call, so reads still run (the
    window frees within a minute)."""
    return max(1, math.floor(limit * READ_SHARE))


def _read_day_limit(limit: int) -> int:
    """A read's share of the day: no minimum, so reads never spend the day's last write (a day
    cap of 1 is a write's alone)."""
    return math.floor(limit * READ_SHARE)


class CallBudget:
    """Sliding-window limits on the calls sent to the broker, writes ahead of reads. Thread-safe.

    `take` is the decision at one instant: 0 when a call may go now (and it is counted), else
    the seconds until one may (infinite when the day's calls are spent). `acquire` waits for a
    call up to a bound. `clock` is monotonic seconds; `day` the trading date the day cap counts.
    """

    def __init__(
        self,
        limits: CallLimits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        day: Callable[[], date] = lambda: trade_date_of(utc_now()),
    ) -> None:
        self.limits = limits or CallLimits()
        self._clock = clock
        self._day = day
        self._cond = threading.Condition()
        self._sent: deque[float] = deque()  # when each call of the last minute was granted
        self._day_of: date | None = None
        self._day_count = 0
        self._paused_until = 0.0
        self._writers = 0

    def take(self, kind: str, now: float, today: date) -> float:
        """Grant one call of `kind` at `now` (0.0), or the seconds to wait for one."""
        with self._cond:
            return self._take(kind, now, today)

    def _take(self, kind: str, now: float, today: date) -> float:
        limits = self.limits
        write = kind == WRITE
        if today != self._day_of:
            self._day_of, self._day_count = today, 0
        if limits.per_day is not None:
            day_limit = limits.per_day if write else _read_day_limit(limits.per_day)
            if self._day_count >= day_limit:
                return math.inf
        while self._sent and self._sent[0] <= now - 60.0:
            self._sent.popleft()
        # the longest of the waits, so a caller that waits as told is granted
        wait = max(self._paused_until - now, 0.0)
        per_second = limits.per_second if write else _read_limit(limits.per_second)
        per_minute = limits.per_minute if write else _read_limit(limits.per_minute)
        last_second = [at for at in self._sent if at > now - 1.0]
        if len(last_second) >= per_second:
            wait = max(wait, last_second[len(last_second) - per_second] + 1.0 - now)
        if len(self._sent) >= per_minute:
            wait = max(wait, self._sent[len(self._sent) - per_minute] + 60.0 - now)
        if wait > 0:
            # a microsecond over, so the call at `now + wait` is past the window's edge whatever
            # the float rounding of `now + wait`
            return wait + _WAIT_SLACK
        self._sent.append(now)
        self._day_count += 1
        return 0.0

    def acquire(self, kind: str, max_wait: float) -> bool:
        """Wait up to `max_wait` seconds for a call of `kind`; True once granted. A read also
        waits while a write is waiting, so a write is never kept behind one."""
        deadline = self._clock() + max_wait
        with self._cond:
            if kind == WRITE:
                self._writers += 1
            try:
                while True:
                    now = self._clock()
                    if kind != WRITE and self._writers:
                        wait = _WRITER_POLL
                    else:
                        wait = self._take(kind, now, self._day())
                        if wait == 0:
                            return True
                    left = deadline - now
                    if left <= 0 or math.isinf(wait):
                        return False
                    self._cond.wait(min(wait, left))
            finally:
                if kind == WRITE:
                    self._writers -= 1
                    self._cond.notify_all()

    def pause(self, seconds: float) -> None:
        """Send nothing for `seconds` (a broker 429), at most `MAX_PAUSE`."""
        seconds = min(max(seconds, 0.0), MAX_PAUSE)
        with self._cond:
            self._paused_until = max(self._paused_until, self._clock() + seconds)


def limits_from(env: Mapping[str, str] | None = None) -> CallLimits:
    """The call limits the settings name (`HatSettings.broker_calls_per_*`)."""
    settings = load_settings(env)
    return CallLimits(
        per_second=settings.broker_calls_per_second,
        per_minute=settings.broker_calls_per_minute,
        per_day=settings.broker_calls_per_day,
    )


_PROCESS_BUDGET: CallBudget | None = None
_PROCESS_BUDGET_LOCK = Lock()


def process_budget() -> CallBudget:
    """The budget every production broker shares: one Fyers app, one set of limits."""
    global _PROCESS_BUDGET
    with _PROCESS_BUDGET_LOCK:
        if _PROCESS_BUDGET is None:
            _PROCESS_BUDGET = CallBudget(limits_from())
        return _PROCESS_BUDGET


# The HTTP status and Retry-After of the last Fyers response on this worker thread, seen by a
# response hook on the SDK's requests session (the SDK hides both from its caller).
_HTTP = threading.local()


def _note_response(response: Any, *_args: object, **_kwargs: object) -> None:  # noqa: ANN401
    _HTTP.status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None) or {}
    _HTTP.retry_after = headers.get("Retry-After")


def _retry_after(raw: object) -> float:
    """Seconds from a Retry-After header in seconds; `THROTTLE_PAUSE` for anything else."""
    try:
        seconds = float(str(raw))
    except (TypeError, ValueError):
        return THROTTLE_PAUSE
    return seconds if math.isfinite(seconds) and seconds >= 0 else THROTTLE_PAUSE


def _watch_responses(model: Any) -> None:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
    """Hook `_note_response` into the model's requests session once (best effort: a model
    without one, such as a test double, is left as it is)."""
    session = getattr(getattr(model, "service", None), "session", None)
    hooks = getattr(session, "hooks", None)
    if not isinstance(hooks, dict):
        return
    response_hooks = hooks.setdefault("response", [])
    if isinstance(response_hooks, list) and _note_response not in response_hooks:
        response_hooks.append(_note_response)


_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fyers-broker")


class _Throttled:
    """A call Fyers answered with HTTP 429 (the budget is already paused)."""


class FyersBroker:
    """Order writes, GTT writes and fail-closed reads over the Fyers SDK model.

    `client` returns the SDK model (resolved on each use, so a re-login is picked up) or None when
    there is no usable session; the default asks the process-wide `FyersSession`.
    """

    def __init__(
        self,
        client: Callable[[], Any] | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        budget: CallBudget | None = None,
    ) -> None:
        self._client_source = client if client is not None else lambda: get_fyers_session().fyers
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        # None sends without a budget (a test double); production passes `process_budget()`
        self.budget = budget

    @property
    def client(self) -> Any:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        """Current Fyers SDK model, or None when there is no usable session."""
        try:
            model = self._client_source()
        except Exception as error:
            logger.warning("Fyers broker not connected (%s)", type(error).__name__)
            return None
        _watch_responses(model)  # every model a call resolves sees its 429s
        return model

    def _call_with_guard(
        self,
        operation: str,
        fn: Callable[[], object],
        *,
        timeout_sec: float = 2.0,
        fallback: object,
        not_sent: object | None = None,
        kind: str = READ,
    ) -> object:
        """`fn` on a worker thread with a timeout; `fallback` when the circuit is open, the call
        times out or raises. Each timeout or failure counts toward opening the circuit. With
        `not_sent`, an open circuit returns it instead: nothing was sent, which a place must not
        read as a lost answer. A call the budget does not grant in time, or one Fyers answers
        with HTTP 429, is answered as a rate-limit refusal (`_throttled`) and counts neither way
        toward the circuit: the broker is up, it only asks HAT to slow down."""
        if self.breaker.is_open():
            return fallback if not_sent is None else not_sent
        budget = self.budget
        if budget is not None and not budget.acquire(
            kind, WRITE_WAIT if kind == WRITE else READ_WAIT
        ):
            return _throttled(operation)
        if self.breaker.is_open():  # opened by another call while this one waited
            return fallback if not_sent is None else not_sent

        def throttled() -> _Throttled:
            # paused here, not by the caller: a 429 that lands after the caller timed out still
            # slows every later call
            if budget is not None:
                budget.pause(_retry_after(_HTTP.retry_after))
            return _Throttled()

        def watched() -> object:
            _HTTP.status = _HTTP.retry_after = None
            try:
                result = fn()
            except Exception:
                if _HTTP.status == HTTP_TOO_MANY_REQUESTS:
                    return throttled()
                raise
            if _HTTP.status == HTTP_TOO_MANY_REQUESTS:
                return throttled()
            return result

        future = _EXECUTOR.submit(watched)
        try:
            result = future.result(timeout=timeout_sec)
            if isinstance(result, _Throttled):
                logger.warning("Fyers operation %s rate limited (HTTP 429)", operation)
                return _throttled(operation)
            self.breaker.record_success()
            return result
        except FutureTimeoutError:
            future.cancel()
            self.breaker.record_failure()
            logger.warning("Fyers operation %s timed out after %.2fs", operation, timeout_sec)
            return fallback
        except Exception as error:
            self.breaker.record_failure()
            future.cancel()
            logger.exception("Fyers operation %s failed", operation, exc_info=error)
            return fallback

    # --- writes: each takes the SDK payload exactly as it goes out ------------------------

    def place_order(self, data: dict[str, Any]) -> OrderReply:
        """Send the order payload `data`. A lost answer is SUBMIT_UNCONFIRMED, never a
        rejection."""
        return self._place(
            "place_order", lambda model: model.place_order(data=data), "Order Placed"
        )

    def place_gtt(self, data: dict[str, Any]) -> OrderReply:
        """Send the single-leg GTT payload `data` (`{side, symbol, productType, orderInfo: {leg1:
        {price, triggerPrice, qty}}, orderTag}`; Fyers shows the tag on the GTT book row as
        `ordertag` "<n>:GTT<tag>", recorded 2026-10-22). Like place_order, a lost answer is
        SUBMIT_UNCONFIRMED."""
        return self._place(
            "place_gtt", lambda model: model.place_gtt_order(data=data), "GTT Placed"
        )

    def _place(
        self, operation: str, call: Callable[[Any], object], default_message: str
    ) -> OrderReply:
        model = self.client
        if not model:
            return _rejected(f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.")
        if self.breaker.is_open():
            # Nothing is sent while the circuit is open: a definite rejection.
            return _rejected(BROKER_CIRCUIT_OPEN)
        return self._send(operation, lambda: call(model), default_message)

    def _send(self, operation: str, call: Callable[[], object], default_message: str) -> OrderReply:
        """A place whose id comes back in `id`: SUBMITTED with an id, REJECTED on an explicit
        error, and SUBMIT_UNCONFIRMED for anything else (the order may have landed)."""
        no_reply: dict[str, Any] = {}
        not_sent: dict[str, Any] = {}
        try:
            response = self._call_with_guard(
                operation, call, timeout_sec=1.5, fallback=no_reply, not_sent=not_sent, kind=WRITE
            )
        except Exception:
            logger.exception("Fyers %s failed", operation)
            return _unconfirmed()
        if response is not_sent:
            # The circuit opened between the caller's check and the call: nothing was sent.
            return _rejected(BROKER_CIRCUIT_OPEN)
        if response is no_reply or not isinstance(response, dict):
            return _unconfirmed()  # timed out or raised mid-request: it may still have landed
        if response.get("s") == "ok":
            order_id = response.get("id")
            if not order_id:
                logger.error("Fyers accepted %s without returning its id", operation)
                return _unconfirmed()
            return OrderReply(
                order_id=str(order_id),
                status="SUBMITTED",
                message=response.get("message", default_message),
                details=response,
            )
        if response.get("s") != "error":
            # Neither ok nor an explicit rejection: the order may have landed.
            logger.error("Fyers %s reply was neither ok nor an error", operation)
            return _unconfirmed()
        return _rejected(response.get("message", "Fyers rejected the order"), response)

    def modify_order(self, order_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Modify a pending order: `fields` are the SDK payload's fields besides its `id`."""
        return self._write(
            "modify_order",
            lambda model: model.modify_order(data={"id": order_id, **fields}),
            "Fyers modify timeout/unavailable",
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._write(
            "cancel_order",
            lambda model: model.cancel_order(data={"id": order_id}),
            "Fyers cancel timeout/unavailable",
        )

    def modify_gtt(self, gtt_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Modify a GTT: `fields` are the SDK payload's fields besides its `id`."""
        return self._write(
            "modify_gtt",
            lambda model: model.modify_gtt_order(data={"id": gtt_id, **fields}),
            "Fyers modify timeout/unavailable",
        )

    def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        return self._write(
            "cancel_gtt",
            lambda model: model.cancel_gtt_order(data={"id": gtt_id}),
            "Fyers cancel timeout/unavailable",
        )

    def _write(self, operation: str, call: Callable[[Any], object], timeout_message: str) -> Any:  # noqa: ANN401
        model = self.client
        if not model:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        return self._call_with_guard(
            operation,
            lambda: call(model),
            timeout_sec=1.5,
            fallback={"s": "error", "message": timeout_message},
            kind=WRITE,
        )

    # --- reads (fail closed) --------------------------------------------------------------

    def _read_ok(self, operation: str, call: Callable[[], object]) -> dict[str, Any]:
        """A Fyers read that must have answered `{"s": "ok"}`.

        Raises FyersUnavailableError when the client is missing, the call timed out or raised, the
        circuit is open, or the reply is not ok, so a caller never reads an outage as an empty book
        (trading-safety rule 7); FyersSessionExpiredError, a subclass, when Fyers refused the token
        (code -16). The error text is fixed, never the broker's own.
        """
        if not self.client:
            raise FyersUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")
        response = self._call_with_guard(operation, call, timeout_sec=2.0, fallback=None)
        return checked_reply(operation, response)

    def get_profile(self) -> dict[str, Any]:
        """The account profile (`data`: `fy_id`, the Fyers user id, and the rest); raises
        FyersSessionExpiredError when Fyers refuses the token, FyersUnavailableError otherwise."""
        if not self.client:
            raise FyersUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")
        reply = self._call_with_guard(
            "profile", lambda: self.client.get_profile(), timeout_sec=2.0, fallback=None
        )
        return profile_data(reply)

    def _read_list(self, operation: str, call: Callable[[], object], key: str) -> list[Any]:
        """The list under `key` of an ok read. An ok reply without that list (missing, null,
        nested elsewhere, not a list) is an unreadable book, never an empty one: a tag lookup
        that read it as empty would send the same order again."""
        rows = self._read_ok(operation, call).get(key)
        if not isinstance(rows, list):
            logger.error("Fyers %s reply has no %s list", operation, key)
            raise FyersUnavailableError(f"Fyers {operation} request failed")
        return list(rows)

    def fetch_positions(self) -> list[dict[str, Any]]:
        """The net position rows as Fyers returns them; raises when they cannot be read."""
        return self._read_list("positions", lambda: self.client.positions(), "netPositions")

    def fetch_orderbook(self) -> list[dict[str, Any]]:
        """Today's orderbook rows as Fyers returns them; raises when it cannot be read."""
        return self._read_list("orderbook", lambda: self.client.orderbook(), "orderBook")

    def fetch_tradebook(self) -> list[dict[str, Any]]:
        """Today's trades; raises when the tradebook cannot be read."""
        return self._read_list("tradebook", lambda: self.client.tradebook(), "tradeBook")

    def fetch_holdings(self) -> list[dict[str, Any]]:
        """Demat holdings; raises when they cannot be read."""
        return self._read_list("holdings", lambda: self.client.holdings(), "holdings")

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        """The GTT order book rows as Fyers returns them; raises when it cannot be read. The
        rows' field names are unverified on a live account [verify]."""
        return self._read_list("gtt_orderbook", lambda: self.client.gtt_orderbook(), "orderBook")

    def fetch_funds(self) -> dict[str, Any]:
        """The ok funds reply as Fyers returns it; raises when it cannot be read."""
        return self._read_ok("funds", lambda: self.client.funds())
