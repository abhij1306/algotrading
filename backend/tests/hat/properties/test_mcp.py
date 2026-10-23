"""PBT mcp.*: the MCP client's session, retries, call log and error
text."""

import logging
from pathlib import Path
from typing import Any

from hat_mcp_support import ScriptedTransport, fixture_json
from hypothesis import assume, example, given
from hypothesis import strategies as st

from app.hat.data import mcp_client
from app.hat.data.mcp_client import (
    MAX_ATTEMPTS,
    HttpTransport,
    McpClient,
    McpConfigError,
    McpError,
    McpTransportError,
    backoff_delay,
    unwrap_result,
)

GOOD = fixture_json("mcp_bulk_quote.json")
RECORDED = {  # replies of the live servers (the breadth one recorded 2026-10-22)
    "get_bulk_quote": GOOD,
    "get_market_breadth": fixture_json("mcp_market_breadth.json"),
}
CODES = [
    mcp_client.CODE_TRANSPORT,
    mcp_client.CODE_TIMEOUT,
    mcp_client.CODE_TOOL_ERROR,
    mcp_client.CODE_BAD_RESPONSE,
]
STABLE_TEXT = {*CODES, mcp_client.CODE_CONFIG}
LOG_FIELD_TEXT = (
    "returned_date",
    "symbol_count",
    "mcp call",
    "mcp_transport",
    "asctime",
    "levelname",
    # the call's own inputs, which the client logs by design (found when PR 29's new string
    # constants shifted Hypothesis's pool and it drew "nse-bhavc")
    "nse-bhavcopy",
    "get_bulk_quote",
)

payloads = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=0x2FF), min_size=9, max_size=60
)
failure = st.one_of(
    st.sampled_from(CODES).map(McpTransportError),
    payloads.map(Exception),
    payloads.map(RuntimeError),
    payloads.map(McpTransportError),  # a transport that wrongly puts text in the code
)
outcomes = st.lists(st.one_of(failure, st.just(GOOD)), min_size=1, max_size=8)


@given(script=outcomes, base=st.floats(min_value=0.001, max_value=10))
def test_retry_bounded(script: list[Any], base: float) -> None:
    """For every sequence of transport failures a logical call makes at most 4 attempts, waits
    strictly longer before each retry, and after the last failure raises a typed error."""
    sleeps: list[float] = []
    transport = ScriptedTransport(script)
    client = McpClient(transport, sleep=sleeps.append, backoff_base=base)
    first_ok = next((i for i, o in enumerate(script) if o is GOOD), None)
    try:
        data = client.call("nse-bhavcopy", "get_bulk_quote", {"symbols": ["TCS"]})
    except McpError as exc:
        assert first_ok is None or first_ok >= MAX_ATTEMPTS
        assert exc.attempts == MAX_ATTEMPTS
        assert len(transport.calls) == MAX_ATTEMPTS
        assert exc.code in STABLE_TEXT
    else:
        assert first_ok is not None
        assert first_ok < MAX_ATTEMPTS
        assert data == GOOD  # never an empty stand-in for a failure
        assert len(transport.calls) == first_ok + 1
    assert len(transport.calls) <= MAX_ATTEMPTS
    assert len(sleeps) == len(transport.calls) - 1
    assert all(later > earlier for earlier, later in zip(sleeps, sleeps[1:], strict=False))
    assert all(s > 0 for s in sleeps)


@given(retry=st.integers(1, 3), base=st.floats(min_value=0.001, max_value=10))
def test_retry_bounded_backoff_grows(retry: int, base: float) -> None:
    assert backoff_delay(retry + 1, base) > backoff_delay(retry, base) > 0


def leaks(payload: str, *texts: str) -> bool:
    """True when any text holds a substring of `payload` longer than 8 characters."""
    return any(payload[i : i + 9] in text for text in texts for i in range(len(payload) - 8))


@example(payload="nse-bhavc", kind="raise")  # a logged server name is not a leak
@example(
    payload=Path(mcp_client.__file__).parents[3].as_posix()[-40:], kind="raise"
)  # a source path is not logged data
@given(
    payload=payloads,
    kind=st.sampled_from(["raise", "code_text", "tool_error", "http_500", "http_raise"]),
)
def test_no_raw_error_text(payload: str, kind: str) -> None:
    """For every error payload the transport produces, no user-facing text (the error's message
    and repr, its chain, the log lines) holds any substring of it longer than 8 characters."""
    assume(not any(payload[i : i + 9] in c for c in STABLE_TEXT for i in range(len(payload) - 8)))
    # fixed log field names and messages are not leaks (Hypothesis draws them from the code's
    # own string constants, so a payload can equal one by construction)
    assume(not leaks(payload, *LOG_FIELD_TEXT))
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": payload}]},
    }

    class Response:
        def __init__(self, status: int, text: str) -> None:
            self.status_code, self.text = status, text

    def post_500(*_a: object, **_k: object) -> Response:
        return Response(500, payload)

    def post_raise(*_a: object, **_k: object) -> Response:
        raise ConnectionError(payload)

    if kind == "raise":
        transport = ScriptedTransport([RuntimeError(payload)])
    elif kind == "code_text":  # a transport that puts server text where a code belongs
        transport = ScriptedTransport([McpTransportError(payload)])
    elif kind == "tool_error":
        transport = ScriptedTransport(lambda *_a: unwrap_result(envelope))
    else:
        transport = HttpTransport(
            {"nse-bhavcopy": "http://fixture.invalid"},
            post=post_500 if kind == "http_500" else post_raise,
        )
    client = McpClient(transport, sleep=lambda _s: None)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    root = logging.getLogger("app.hat")
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        try:
            client.call("nse-bhavcopy", "get_bulk_quote", {"symbols": ["TCS"]})
        except McpError as exc:
            texts = [str(exc), repr(exc), exc.code, *map(str, exc.args)]
            assert exc.__cause__ is None
            assert exc.__context__ is None
            assert not leaks(payload, *texts)
            assert exc.code in STABLE_TEXT
        else:
            raise AssertionError("an error payload must raise")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
    assert records, "the call must have logged something for this check to mean anything"
    # The record's own source location (`pathname`, `filename`, `module`) is not data the client
    # logged: on a runner it holds the checkout directory ("algotrading"), which a generated
    # payload can match by chance (found when a new module shifted Hypothesis's string pool).
    place = {"pathname", "filename", "module"}
    logged = [
        r.getMessage() + repr({k: v for k, v in r.__dict__.items() if k not in place})
        for r in records
    ]
    assert not leaks(payload, *logged)


@given(failures=st.integers(0, 3))
def test_retry_bounded_config_error_not_retried(failures: int) -> None:
    """A configuration error is not transient: it is raised at once, never retried, and keeps
    its own code instead of becoming a transport failure."""
    sleeps: list[float] = []
    transport = ScriptedTransport([McpTransportError()] * failures + [McpConfigError()])
    client = McpClient(transport, sleep=sleeps.append)
    try:
        client.call("nse-bhavcopy", "get_bulk_quote", {"symbols": ["TCS"]})
    except McpConfigError as exc:
        assert exc.code == mcp_client.CODE_CONFIG
    else:
        raise AssertionError("a configuration error must raise")
    assert len(transport.calls) == failures + 1
