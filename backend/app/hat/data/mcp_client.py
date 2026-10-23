"""A thin JSON-RPC-over-HTTP MCP client for the two vetted NSE servers (HAT-DATA-030..036).

Every call goes through an `McpTransport`, so tests replace the network with recorded fixtures.
`McpClient` adds what the spec requires around it: at most 4 calls in flight (HAT-DATA-032),
at most 3 retries with strictly growing backoff (HAT-DATA-033), one log record per attempt
(HAT-DATA-034), typed errors that never carry server text (HAT-DATA-035) and a date check
that reports rows for another day instead of accepting them (HAT-DATA-036).

Only `HttpTransport` here opens a connection to an MCP endpoint (CONF `imports.mcp_transport_only`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, TypeVar

from ..errors import HatError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# HAT-DEC-I16: the only servers HAT code can address; research MCPs are chat-side only.
SERVER_BHAVCOPY = "nse-bhavcopy"
SERVER_CMMARKET = "cm-market"
ALLOWED_SERVERS = frozenset({SERVER_BHAVCOPY, SERVER_CMMARKET})
ENDPOINT_ENV = {
    SERVER_BHAVCOPY: "HAT_MCP_BHAVCOPY_URL",
    SERVER_CMMARKET: "HAT_MCP_CMMARKET_URL",
}

MAX_RETRIES = 3
MAX_ATTEMPTS = MAX_RETRIES + 1
MAX_CONCURRENCY = 4
POOL_THREADS = 8  # worker threads; never the concurrency limit (the semaphore is)
BATCH_SIZE = 50
PROBE_SYMBOL = "RELIANCE"  # HAT-DATA-040: the one symbol the readiness probe asks for
BACKOFF_BASE_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0
MCP_PROTOCOL_VERSION = "2025-03-26"
HTTP_SESSION_LOST = 404  # streamable HTTP: the server no longer knows the session id
HANDSHAKE_TRIES = 2

# Stable error codes: the only text an alert, API response or Telegram message may carry.
CODE_TRANSPORT = "mcp_transport"
CODE_TIMEOUT = "mcp_timeout"
CODE_TOOL_ERROR = "mcp_tool_error"
CODE_BAD_RESPONSE = "mcp_bad_response"
CODE_CONFIG = "mcp_config"
# `get_index_history` asked for a window before the server's depth (about two years): the end of
# that index's chain, not a failure (HAT-DATA-095). Only the stable code leaves the client.
CODE_NO_DATA = "mcp_no_data"
NO_DATA_PREFIX = "No data for index"
TRANSPORT_CODES = frozenset({CODE_TRANSPORT, CODE_TIMEOUT, CODE_TOOL_ERROR, CODE_BAD_RESPONSE})


class McpError(HatError):
    """An MCP call failed. `code` is stable; no server or network text is ever kept."""

    def __init__(self, code: str, *, attempts: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.attempts = attempts


class McpConfigError(McpError):
    """An unknown server, or an endpoint that is not configured."""

    def __init__(self) -> None:
        super().__init__(CODE_CONFIG)


class McpTransportError(Exception):
    """Raised by a transport. `code` is one of the stable codes; the message is discarded."""

    def __init__(self, code: str = CODE_TRANSPORT) -> None:
        self.code = code if code in TRANSPORT_CODES else CODE_TRANSPORT
        super().__init__(self.code)


class McpTransport(Protocol):
    """Sends one `tools/call` and returns the tool's JSON result (already unwrapped)."""

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object: ...


def endpoints_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """Configured endpoint per allowed server; a blank or missing value means unconfigured."""
    found = {}
    for server, var in ENDPOINT_ENV.items():
        value = (env.get(var) or "").strip()
        if value:
            found[server] = value
    return found


class HttpTransport:
    """MCP over streamable HTTP: one session per server, opened with `initialize` and
    `notifications/initialized`, its `Mcp-Session-Id` sent on every `tools/call`. A 404 means the
    server lost the session (the NSE servers run several replicas): the call opens a new session
    and is sent once more. Never constructed by tests with a real network."""

    def __init__(
        self,
        endpoints: Mapping[str, str],
        post: Callable[..., Any] | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        unknown = set(endpoints) - ALLOWED_SERVERS
        if unknown:
            raise McpConfigError
        self._endpoints = dict(endpoints)
        self._post = post
        self._timeout = timeout
        self._ids = iter(range(1, 1 << 62))
        self._id_lock = threading.Lock()
        self._sessions: dict[str, _McpSession] = {}
        # one lock per server: a slow handshake with one server never holds up calls to the other
        self._session_locks = {server: threading.Lock() for server in self._endpoints}

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object:
        url = self._endpoints.get(server)
        if url is None:
            raise McpConfigError
        params = {"name": tool, "arguments": dict(arguments)}
        session = self._session(server, url, stale=None)
        status, text = self._send(url, "tools/call", params, session)
        if status == HTTP_SESSION_LOST:
            session = self._session(server, url, stale=session)
            status, text = self._send(url, "tools/call", params, session)
            if status == HTTP_SESSION_LOST:
                self._forget(server, session)
        if status >= 400:
            raise McpTransportError(CODE_TRANSPORT)
        return unwrap_result(_decode_body(text))

    def _session(self, server: str, url: str, *, stale: _McpSession | None) -> _McpSession:
        """The server's open session; a new one when there is none or it is `stale` (the one a
        call just saw lost). Concurrent calls to the server share one handshake."""
        with self._session_locks[server]:
            current = self._sessions.get(server)
            if current is not None and current is not stale:
                return current
            self._sessions.pop(server, None)
            opened = self._handshake(url)
            self._sessions[server] = opened
            return opened

    def _forget(self, server: str, session: _McpSession) -> None:
        """Drop `session` when it is still the server's open one: the next call opens another."""
        with self._session_locks[server]:
            if self._sessions.get(server) is session:
                del self._sessions[server]

    def _handshake(self, url: str) -> _McpSession:
        """Open and initialize a session. A 404 on either step means another replica answered,
        so the handshake starts once more; any other failure, or a JSON-RPC error answering
        `initialize`, is a transport error."""
        for _try in range(HANDSHAKE_TRIES):
            session = self._try_handshake(url)
            if session is not None:
                return session
        raise McpTransportError(CODE_TRANSPORT)

    def _try_handshake(self, url: str) -> _McpSession | None:
        status, text, headers = self._post_raw(
            url,
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "hat", "version": "1"},
                },
            },
            {},
        )
        if status == HTTP_SESSION_LOST:
            return None
        session_id = _header(headers, "mcp-session-id")
        if status >= 400 or not session_id:
            raise McpTransportError(CODE_TRANSPORT)
        envelope = _decode_body(text)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
            raise McpTransportError(CODE_TRANSPORT)  # an error answer opens no session
        agreed = envelope["result"].get("protocolVersion")
        version = agreed if isinstance(agreed, str) and agreed else MCP_PROTOCOL_VERSION
        session = _McpSession(session_id, version)
        status, _text, _headers = self._post_raw(
            url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session.headers()
        )
        if status == HTTP_SESSION_LOST:
            return None
        if status >= 400:
            raise McpTransportError(CODE_TRANSPORT)
        return session

    def _send(
        self, url: str, method: str, params: Mapping[str, Any], session: _McpSession
    ) -> tuple[int, str]:
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        status, text, _headers = self._post_raw(url, body, session.headers())
        return status, text

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    def _post_raw(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> tuple[int, str, Mapping[str, str]]:
        post = self._post
        if post is None:
            import requests  # deferred: only a real call needs the HTTP stack

            post = requests.post
        try:
            response = post(
                url,
                json=body,
                headers={"Accept": "application/json, text/event-stream", **headers},
                timeout=self._timeout,
            )
            status = int(response.status_code)
            text = response.text
            reply_headers = getattr(response, "headers", None) or {}
        except TimeoutError as exc:
            raise McpTransportError(CODE_TIMEOUT) from exc
        except Exception as exc:  # any network or client failure: the text is dropped
            timed_out = type(exc).__name__.endswith("Timeout")
            raise McpTransportError(CODE_TIMEOUT if timed_out else CODE_TRANSPORT) from exc
        return status, text, reply_headers


@dataclass(frozen=True)
class _McpSession:
    session_id: str
    protocol_version: str

    def headers(self) -> dict[str, str]:
        return {"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": self.protocol_version}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """A reply header by case-insensitive name."""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value) if value else None
    return None


def _decode_body(text: str) -> object:
    """A JSON body, or the last `data:` line of an event stream."""
    try:
        stripped = text.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        data_lines = [
            line[5:].strip() for line in stripped.splitlines() if line.startswith("data:")
        ]
        return json.loads(data_lines[-1])
    except (ValueError, IndexError) as exc:
        raise McpTransportError(CODE_BAD_RESPONSE) from exc


def unwrap_result(envelope: object) -> object:
    """The tool's JSON payload out of a JSON-RPC response; any failure is a typed error."""
    if not isinstance(envelope, dict) or "error" in envelope:
        raise McpTransportError(CODE_TOOL_ERROR)
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise McpTransportError(CODE_BAD_RESPONSE)
    if result.get("isError"):
        raise McpTransportError(CODE_TOOL_ERROR)
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise McpTransportError(CODE_BAD_RESPONSE)
    try:
        return json.loads(content[0].get("text", ""))
    except (TypeError, ValueError) as exc:
        raise McpTransportError(CODE_BAD_RESPONSE) from exc


@dataclass(frozen=True)
class CallRecord:
    """One attempt (HAT-DATA-034)."""

    server: str
    tool: str
    symbol_count: int
    attempt: int
    returned_date: date | None
    failure: str | None


@dataclass
class CallLog:
    """The attempts of one job run; `calls` feeds `hat_job_run.calls`."""

    records: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)
        logger.info(
            "mcp call",
            extra={
                "server": record.server,
                "tool": record.tool,
                "symbols": record.symbol_count,
                "attempt": record.attempt,
                "returned_date": record.returned_date.isoformat() if record.returned_date else None,
                "failure": record.failure,
            },
        )

    @property
    def calls(self) -> int:
        return len(self.records)


def backoff_delay(retry: int, base: float = BACKOFF_BASE_SECONDS) -> float:
    """Delay before retry number `retry` (1-based): strictly increasing in `retry`."""
    return base * (2 ** (retry - 1))


def batches(items: Sequence[T], size: int = BATCH_SIZE) -> list[list[T]]:
    """Partition `items` in order into batches of at most `size` (HAT-DATA-031)."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def parse_data_date(value: object) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class QuoteBatch:
    """Rows of one bulk-quote call split by date: `rows` match the requested trade date."""

    rows: list[dict[str, Any]]
    mismatched: list[dict[str, Any]]
    missing: list[str]  # requested symbols the server returned nothing usable for


class McpClient:
    def __init__(
        self,
        transport: McpTransport,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
        sleep: Callable[[float], None] = time.sleep,
        log: CallLog | None = None,
        backoff_base: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._transport = transport
        self._max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._sleep = sleep
        self._backoff_base = backoff_base
        self.log = log if log is not None else CallLog()

    def call(
        self,
        server: str,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        symbol_count: int = 0,
        date_of: Callable[[object], date | None] | None = None,
    ) -> object:
        """One logical call: up to 4 attempts, one slot held per attempt, never an empty result
        on failure. `date_of` extracts the returned data date for the log."""
        if server not in ALLOWED_SERVERS:
            raise McpConfigError
        code = CODE_TRANSPORT
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                self._sleep(backoff_delay(attempt - 1, self._backoff_base))
            with self._slots:
                try:
                    data = self._transport.call(server, tool, arguments)
                except McpConfigError:
                    raise  # not a transient failure: never retried, never renamed
                except McpTransportError as exc:
                    code = exc.code
                except Exception:  # a transport bug is still a failed attempt, never raw text
                    code = CODE_TRANSPORT
                else:
                    self.log.add(
                        CallRecord(
                            server, tool, symbol_count, attempt, _safe_date(date_of, data), None
                        )
                    )
                    return data
            self.log.add(CallRecord(server, tool, symbol_count, attempt, None, code))
        raise McpError(code, attempts=MAX_ATTEMPTS)

    def map_calls(self, work: Iterable[Callable[[], T]]) -> list[T]:
        """Run the callables on a thread pool, results in input order. The pool is deliberately
        wider than the limit: the semaphore in `call` alone bounds the calls in flight."""
        thunks = list(work)
        if not thunks:
            return []
        with ThreadPoolExecutor(max_workers=POOL_THREADS) as pool:
            return [f.result() for f in [pool.submit(t) for t in thunks]]

    def bulk_quote(self, symbols: Sequence[str], trade_date: date) -> QuoteBatch:
        """`get_bulk_quote` for at most 50 symbols. Rows whose `date` is not `trade_date` are
        reported in `mismatched`, never in `rows` (HAT-DATA-036)."""
        if len(symbols) > BATCH_SIZE:
            raise ValueError("a bulk quote takes at most 50 symbols")
        data = self.call(
            SERVER_BHAVCOPY,
            "get_bulk_quote",
            {"symbols": list(symbols)},
            symbol_count=len(symbols),
            date_of=_quote_date,
        )
        quotes = data.get("quotes") if isinstance(data, dict) else None
        if not isinstance(quotes, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        rows: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for quote in quotes:
            if not isinstance(quote, dict) or not isinstance(quote.get("symbol"), str):
                continue
            seen.add(quote["symbol"])
            if parse_data_date(quote.get("date")) == trade_date:
                rows.append(quote)
            else:
                mismatched.append(quote)
        return QuoteBatch(rows, mismatched, [s for s in symbols if s not in seen])

    def bulk_quotes(self, symbols: Sequence[str], trade_date: date) -> QuoteBatch:
        """All symbols in batches of 50, up to 4 batches in flight; results merged in order."""
        parts = self.map_calls(
            [(lambda b=b: self.bulk_quote(b, trade_date)) for b in batches(symbols)]
        )
        return QuoteBatch(
            [r for p in parts for r in p.rows],
            [r for p in parts for r in p.mismatched],
            [s for p in parts for s in p.missing],
        )

    def equity_stocks(self, prefix: str, limit: int) -> list[dict[str, Any]]:
        """`cm_get_equity_stocks` for one symbol prefix (HAT-DATA-023)."""
        data = self.call(
            SERVER_CMMARKET,
            "cm_get_equity_stocks",
            {"limit": limit, "symbolFilter": prefix},
            symbol_count=0,
        )
        stocks = data.get("stocks") if isinstance(data, dict) else None
        if not isinstance(stocks, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        return [s for s in stocks if isinstance(s, dict) and isinstance(s.get("symbol"), str)]

    def corporate_actions(self, symbol: str, from_date: date, to_date: date) -> CorporateActionPage:
        """`get_corporate_actions` for one symbol (HAT-DATA-094). An `error` answer is a failed
        call, never "no actions"; a `warning` marks the list incomplete."""
        data = self.call(
            SERVER_BHAVCOPY,
            "get_corporate_actions",
            {"symbol": symbol, "fromDate": from_date.isoformat(), "toDate": to_date.isoformat()},
            symbol_count=1,
        )
        if not isinstance(data, dict) or "error" in data:
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        rows = [a for a in actions if isinstance(a, dict)]
        return CorporateActionPage(
            rows, incomplete="warning" in data, unreadable=len(actions) - len(rows)
        )

    def probe(self, trade_date: date, symbol: str = PROBE_SYMBOL) -> date | None:
        """The data date `get_bulk_quote([symbol])` answers with, or None when it answers with
        no readable date (HAT-DATA-040). One call; the caller compares it with `trade_date`."""
        batch = self.bulk_quote([symbol], trade_date)
        for quote in [*batch.rows, *batch.mismatched]:
            found = parse_data_date(quote.get("date"))
            if found is not None:
                return found
        return None

    def index_snapshot(self, trade_date: date) -> IndexSnapshot:
        """`get_index_snapshot` for `trade_date`. The server answers with the nearest earlier
        trading day on a holiday, so a different `date` is reported, never stored (HAT-DATA-036,
        044). [A] the answer is `{"date": ..., "indices": [{indexName, open, ..., close}]}`."""
        data = self.call(
            SERVER_BHAVCOPY,
            "get_index_snapshot",
            {"date": trade_date.isoformat(), "filter": ""},
            symbol_count=0,
            date_of=lambda d: parse_data_date(d.get("date")) if isinstance(d, dict) else None,
        )
        if not isinstance(data, dict):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        rows = data.get("indices", data.get("data"))
        if not isinstance(rows, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        return IndexSnapshot(
            parse_data_date(data.get("date")), [r for r in rows if isinstance(r, dict)]
        )

    def market_breadth(self, trade_date: date) -> BreadthSnapshot:
        """`get_market_breadth` for `trade_date` (HAT-DATA-044). [A] the answer carries
        `date`, `total_stocks`, `advances`, `declines`, `unchanged`, `ad_ratio`, `total_volume`
        (snake_case, recorded live 2026-10-22)."""
        data = self.call(
            SERVER_BHAVCOPY,
            "get_market_breadth",
            {"date": trade_date.isoformat()},
            symbol_count=0,
            date_of=lambda d: parse_data_date(d.get("date")) if isinstance(d, dict) else None,
        )
        if not isinstance(data, dict) or "error" in data:
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        return BreadthSnapshot(parse_data_date(data.get("date")), data)

    def stock_history(self, symbol: str, end_date: date, months: int = 1) -> list[dict[str, Any]]:
        """`get_stock_history` rows (raw, unadjusted) for the window ending at `end_date`; used
        by the build's validation sample (HAT-DATA-096)."""
        data = self.call(
            SERVER_BHAVCOPY,
            "get_stock_history",
            {"symbol": symbol, "months": months, "endDate": end_date.isoformat()},
            symbol_count=1,
        )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        return [r for r in rows if isinstance(r, dict)]

    def index_history(
        self, index_name: str, end_date: date, months: int = 12
    ) -> tuple[list[dict[str, Any]], date | None]:
        """One chunk of `get_index_history` and the `next_end_date` to chain from. A window older
        than the server keeps raises `mcp_no_data`; any other unusable answer `mcp_bad_response`."""
        data = self.call(
            SERVER_BHAVCOPY,
            "get_index_history",
            {"indexName": index_name, "months": months, "endDate": end_date.isoformat()},
            symbol_count=1,
        )
        if isinstance(data, dict) and str(data.get("error", "")).startswith(NO_DATA_PREFIX):
            raise McpError(CODE_NO_DATA, attempts=1)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise McpError(CODE_BAD_RESPONSE, attempts=1)
        return (
            [r for r in rows if isinstance(r, dict)],
            parse_data_date(data.get("next_end_date")),
        )


@dataclass(frozen=True)
class IndexSnapshot:
    """`get_index_snapshot` rows and the data date the server answered with."""

    data_date: date | None
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class BreadthSnapshot:
    """`get_market_breadth` fields and the data date the server answered with."""

    data_date: date | None
    fields: dict[str, Any]


@dataclass(frozen=True)
class CorporateActionPage:
    actions: list[dict[str, Any]]
    incomplete: bool
    unreadable: int = 0  # rows that were not an object: an action that could not be read


def client_from_env(env: Mapping[str, str]) -> McpClient | None:
    """The real client for an operator run (the history build CLI), or None when no endpoint is
    configured; tests never call this."""
    endpoints = endpoints_from_env(env)
    return McpClient(HttpTransport(endpoints)) if endpoints else None


def _quote_date(data: object) -> date | None:
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict):
        return parse_data_date(quotes[0].get("date"))
    return None


def _safe_date(date_of: Callable[[object], date | None] | None, data: object) -> date | None:
    if date_of is None:
        return None
    try:
        return date_of(data)
    except Exception:
        return None
