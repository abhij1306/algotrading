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
BACKOFF_BASE_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0

# Stable error codes: the only text an alert, API response or Telegram message may carry.
CODE_TRANSPORT = "mcp_transport"
CODE_TIMEOUT = "mcp_timeout"
CODE_TOOL_ERROR = "mcp_tool_error"
CODE_BAD_RESPONSE = "mcp_bad_response"
CODE_CONFIG = "mcp_config"
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
    """JSON-RPC `tools/call` over HTTP POST. Never constructed by tests."""

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

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object:
        url = self._endpoints.get(server)
        if url is None:
            raise McpConfigError
        post = self._post
        if post is None:
            import requests  # deferred: only a real call needs the HTTP stack

            post = requests.post
        with self._id_lock:
            request_id = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        try:
            response = post(
                url,
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=self._timeout,
            )
            status = int(response.status_code)
            text = response.text
        except TimeoutError as exc:
            raise McpTransportError(CODE_TIMEOUT) from exc
        except Exception as exc:  # any network or client failure: the text is dropped
            timed_out = type(exc).__name__.endswith("Timeout")
            raise McpTransportError(CODE_TIMEOUT if timed_out else CODE_TRANSPORT) from exc
        if status >= 400:
            raise McpTransportError(CODE_TRANSPORT)
        return unwrap_result(_decode_body(text))


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
