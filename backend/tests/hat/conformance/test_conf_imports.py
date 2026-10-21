"""CONF checks of what HAT code may reach: only the two vetted NSE MCP servers, only through
`McpTransport` (HAT-DATA-010, HAT-DATA-030, HAT-DEC-I16)."""

import ast
import inspect
import re

import pytest
from hat_conf_repo import APP_ROOT, build_graph, reaches
from hat_conf_spec import SPEC_DIR

from app.hat.data import mcp_client
from app.hat.data.mcp_client import (
    ALLOWED_SERVERS,
    ENDPOINT_ENV,
    HttpTransport,
    McpClient,
    McpConfigError,
    endpoints_from_env,
)

HAT_ROOT = APP_ROOT / "hat"
HTTP_LIBS = (
    "requests",
    "httpx",
    "httpx2",
    "aiohttp",
    "urllib.request",
    "http.client",
    "websockets",
)
RESEARCH_CLIENTS = ("mcp", "tapetide", "anthropic")
SERVER_NAME = re.compile(r"^(nse|cm)-[a-z0-9-]+$|tapetide", re.IGNORECASE)


def _hat_sources() -> dict[str, ast.AST]:
    return {
        path.relative_to(HAT_ROOT).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(HAT_ROOT.rglob("*.py"))
    }


def _strings(tree: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_imports__engine_data_sources() -> None:
    """HAT-DATA-010: every MCP server name a HAT module can address is nse-bhavcopy or cm-market,
    and engine, risk and execution code import no research or chat MCP client."""
    assert ALLOWED_SERVERS == {"nse-bhavcopy", "cm-market"}
    for name, tree in _hat_sources().items():
        for text in _strings(tree):
            if SERVER_NAME.search(text) and "\n" not in text and len(text) < 40:
                assert text in ALLOWED_SERVERS, f"{name}: {text!r}"
    graph = build_graph()
    for area in ("app.hat.engine", "app.hat.risk", "app.hat.execution"):
        for module in graph:
            if module == area or module.startswith(area + "."):
                assert reaches(graph, module, RESEARCH_CLIENTS) == [], module


def test_imports__mcp_transport_only() -> None:
    """HAT-DATA-030: `McpClient` takes an `McpTransport`; `HttpTransport` is the one place that
    opens a connection to an MCP endpoint; no other HAT module touches an MCP endpoint, its env
    variable or the `tools/call` method, or imports an HTTP library while doing so."""
    parameter = inspect.signature(McpClient.__init__).parameters["transport"]
    assert "McpTransport" in str(parameter.annotation)
    client_tree = _hat_sources()["data/mcp_client.py"]
    top_level_imports = {
        alias.name
        for node in client_tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {
        node.module for node in client_tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(lib in top_level_imports for lib in HTTP_LIBS), (
        "the client imports HTTP at top level"
    )
    for name, tree in _hat_sources().items():
        if name == "data/mcp_client.py":
            continue
        strings = _strings(tree)
        mentions_mcp = (
            any("HAT_MCP_" in s or s == "tools/call" for s in strings)
            or ALLOWED_SERVERS & strings
            or any(isinstance(n, ast.Name) and n.id == "HttpTransport" for n in ast.walk(tree))
        )
        assert not mentions_mcp, f"{name} addresses an MCP endpoint outside the client"
        if "mcp" in name:
            imported = {
                alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
            }
            assert not any(lib in imported for lib in HTTP_LIBS), name
    assert "requests" in inspect.getsource(HttpTransport.call)


def test_mcp_endpoints_allowlist() -> None:
    """HAT-DEC-I16: the client can be configured only with nse-bhavcopy and cm-market; the
    environment variables are exactly the two the ops spec lists."""
    assert set(ENDPOINT_ENV) == set(ALLOWED_SERVERS)
    ops = (SPEC_DIR / "10-ops.md").read_text(encoding="utf-8")
    assert set(re.findall(r"`(HAT_MCP_\w+)`", ops)) == set(ENDPOINT_ENV.values())
    env = {"HAT_MCP_BHAVCOPY_URL": "http://a.invalid", "HAT_MCP_TAPETIDE_URL": "http://b.invalid"}
    assert endpoints_from_env(env) == {"nse-bhavcopy": "http://a.invalid"}
    with pytest.raises(McpConfigError):
        HttpTransport({"tapetide": "http://b.invalid"})
    calls: list[str] = []

    class Spy:
        def call(self, server: str, tool: str, arguments: object) -> object:
            calls.append(server)
            return {}

    client = McpClient(Spy())
    with pytest.raises(McpConfigError):
        client.call("tapetide", "search", {})
    assert calls == []
    assert mcp_client.ALLOWED_SERVERS == ALLOWED_SERVERS


# --- one live feed (HAT-DATA-110, HAT-OVR-006, HAT-DEC-I3) ----------------------------------

SOCKET_MODULES = (
    "fyers_apiv3",
    "app.services.fyers_websocket",
    "app.services.live.data_socket",
    "websocket",
    "websockets",
    "socketio",
    "aiohttp",
)
SOCKET_NAMES = ("FyersDataSocket", "FyersSocket", "data_ws", "ws_connect", "WebSocketApp")


def test_imports__no_second_websocket() -> None:
    """HAT-DATA-110: no module under `app.hat` imports or constructs a broker data-socket client
    or any WebSocket library; HAT's one tick input is `LiveMarketService.add_tick_listener`, which
    `LiveData.attach` calls, and the live service has that hook."""
    from hat_conf_repo import imports_of

    from app.hat.data import live
    from app.services.live_market_service import LiveMarketService

    offenders = []
    for path in sorted(HAT_ROOT.rglob("*.py")):
        for name in imports_of(path):
            if any(name == m or name.startswith(m + ".") for m in SOCKET_MODULES):
                offenders.append((path.name, name))
        text = path.read_text(encoding="utf-8")
        offenders.extend((path.name, n) for n in SOCKET_NAMES if re.search(rf"\b{n}\b", text))
    assert offenders == []
    assert callable(getattr(LiveMarketService, "add_tick_listener", None))
    assert "add_tick_listener" in inspect.getsource(live.LiveData.attach)
    tree = _hat_sources()["data/live.py"]
    attached = [
        n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "add_tick_listener"
    ]
    assert len(attached) == 1  # the single tick input


def test_single_feed_import_rule() -> None:
    """HAT-OVR-006, HAT-DEC-I3: live data reaches HAT only through the listener hook and the
    `"hat"` owner of `SubscriptionRegistry`; HAT imports neither the live service module nor the
    provider session, so it cannot read or open a feed any other way."""
    from hat_conf_repo import imports_of

    from app.hat.data import subscriptions

    assert subscriptions.HAT_OWNER == "hat"
    forbidden = ("app.services.live_market_service", "app.services.live.provider_session")
    for path in sorted(HAT_ROOT.rglob("*.py")):
        names = imports_of(path)
        assert not any(n == f or n.startswith(f + ".") for n in names for f in forbidden), path.name
    owners = {
        node.value
        for node in ast.walk(_hat_sources()["data/subscriptions.py"])
        if isinstance(node, ast.Constant) and node.value == "hat"
    }
    assert owners == {"hat"}
    assert "SubscriptionRegistry" in inspect.getsource(subscriptions.LiveSubscriptions)
