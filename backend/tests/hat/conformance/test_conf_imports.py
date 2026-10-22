"""CONF checks of what HAT code may reach: only the two vetted NSE MCP servers, only through
`McpTransport` (HAT-DATA-010, HAT-DATA-030, HAT-DEC-I16)."""

import ast
import inspect
import re
from pathlib import Path

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
    assert "requests" in inspect.getsource(HttpTransport)  # deferred, inside the transport only


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

SOCKET_MODULES = ("fyers_apiv3", "websocket", "websockets", "socketio", "aiohttp")
# The SDK's REST models (orders, reads, login); the session and login modules import it (K2). The
# sockets live in `fyers_apiv3.FyersWebsocket`, which only the provider module may import (K3).
REST_MODEL = "fyers_apiv3.fyersModel"
PROVIDER_SOCKET_FILE = "data/provider/fyers_websocket.py"
SOCKET_NAMES = ("FyersDataSocket", "FyersSocket", "data_ws", "ws_connect", "WebSocketApp")
# Who may import the feed: the app's wiring (K5b deleted the legacy read side of its ticks).
FEED_IMPORTERS = {"main.py"}
# Outside the provider package, the one module that may import from it, and the one name it may
# take: the minute-history download asks the Fyers session's REST history through it (HAT-DATA-119,
# PR 41). It builds no socket and no provider session.
PROVIDER_IMPORTS = {
    "hat/data/minute_history.py": {
        "app.hat.data.provider.fyers_websocket",
        "app.hat.data.provider.fyers_websocket.fyers_history",
    }
}


def _only_rest_model(path: Path) -> bool:
    """Every `fyers_apiv3` import in `path` is `from fyers_apiv3 import fyersModel`."""
    lines = re.findall(r"^\s*(?:from|import)\s+fyers_apiv3.*$", path.read_text("utf-8"), re.M)
    return all(re.fullmatch(r"\s*from fyers_apiv3 import fyersModel\s*", ln) for ln in lines)


def socket_offenders(
    root: Path, provider_file: Path | None, modules: tuple[str, ...] = SOCKET_MODULES
) -> list[tuple[str, str]]:
    """(file, what) for every import, construction or dynamic import of a broker data socket or a
    WebSocket client library under `root`, except the one provider module `provider_file`."""
    from hat_conf_repo import imports_of

    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == provider_file:
            continue
        for name in imports_of(path):
            if name in {"fyers_apiv3", REST_MODEL} and _only_rest_model(path):
                continue  # the order and login models, not a socket
            if any(name == m or name.startswith(m + ".") for m in modules):
                offenders.append((path.name, name))
        text = path.read_text(encoding="utf-8")
        offenders.extend((path.name, n) for n in SOCKET_NAMES if re.search(rf"\b{n}\b", text))
        # a dynamic import (`importlib.import_module("websockets")`, `__import__`) names the
        # library as a string; the AST import scan does not see it
        offenders.extend(
            (path.name, m) for m in modules if re.search(rf"[\"']{re.escape(m)}[\"'.]", text)
        )
    return offenders


def test_imports__no_second_websocket() -> None:
    """HAT-DATA-110: the Fyers data-socket SDK is imported or constructed in exactly one module,
    `app/hat/data/provider/fyers_websocket.py`, and no module under `app` imports or opens any
    other broker socket or WebSocket client library; HAT's one tick input is
    `LiveFeed.add_tick_listener`, which `LiveData.attach` calls."""
    from app.hat.data import live
    from app.hat.data.feed import LiveFeed

    provider = HAT_ROOT / PROVIDER_SOCKET_FILE
    assert provider.is_file()
    assert socket_offenders(HAT_ROOT, provider) == []
    # the legacy code outside HAT has no broker socket either (it may use fastapi's server sockets)
    legacy = [
        o
        for path in sorted(APP_ROOT.iterdir())
        if path.is_dir() and path.name != "hat"
        for o in socket_offenders(path, None, ("fyers_apiv3",))
    ]
    assert legacy == []
    # the provider module is the one place the SDK socket appears, and it does appear there
    assert any(n in provider.read_text("utf-8") for n in SOCKET_NAMES)
    assert callable(getattr(LiveFeed, "add_tick_listener", None))
    assert "add_tick_listener" in inspect.getsource(live.LiveData.attach)
    tree = _hat_sources()["data/live.py"]
    attached = [
        n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "add_tick_listener"
    ]
    assert len(attached) == 1  # the single tick input


def _constructs(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
        for n in ast.walk(tree)
    )


def _calls_named(node: ast.AST, name: str) -> int:
    return sum(
        isinstance(n, ast.Call) and getattr(n.func, "id", "") == name for n in ast.walk(node)
    )


def _enabled_calls(stmts: list[ast.stmt], callee: str) -> int:
    """Calls of `callee` in `stmts` that run only when HAT is enabled: a nested `if` that tests
    `is_enabled()` negated counts its `else` branch only, any other nested `if` both branches."""
    total = 0
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            negated = isinstance(stmt.test, ast.UnaryOp) and isinstance(stmt.test.op, ast.Not)
            if negated and _calls_named(stmt.test, "is_enabled"):
                total += _enabled_calls(stmt.orelse, callee)
            else:
                total += _enabled_calls(stmt.body, callee) + _enabled_calls(stmt.orelse, callee)
        else:
            total += _calls_named(stmt, callee)
    return total


def _guarded_by_hat_enabled(tree: ast.AST, callee: str) -> bool:
    """Every call of `callee` sits in the body of an `if is_enabled()` (not a negated test, and
    not under a nested `if not is_enabled()`), so it is reachable only on the enabled branch."""
    guarded = 0
    for node in ast.walk(tree):
        positive = (
            isinstance(node, ast.If)
            and _calls_named(node.test, "is_enabled")
            and not (isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not))
        )
        if positive:
            guarded += _enabled_calls(node.body, callee)
    calls = _calls_named(tree, callee)
    return calls > 0 and calls == guarded


def test_single_feed_import_rule() -> None:
    """HAT-OVR-006, HAT-DEC-I3: one provider session and one feed per process. Only
    `app/hat/data/feed.py` builds a `ProviderSession` or imports the provider session module; only
    `app/main.py` builds a `LiveFeed`, and only inside `if is_enabled()`; no other HAT module
    imports the feed or the provider (live data reaches `LiveData` through the listener hook and
    the registry's `"hat"` owner); the feed is imported only by `FEED_IMPORTERS`."""
    from hat_conf_repo import imports_of

    from app.hat.data import subscriptions

    assert subscriptions.HAT_OWNER == "hat"
    provider = "app.hat.data.provider"
    feed = "app.hat.data.feed"
    session_module = "app.hat.data.provider.provider_session"
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        names = imports_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside_provider = rel.startswith("hat/data/provider/")
        if rel != "hat/data/feed.py" and not inside_provider:
            assert not any(n == session_module for n in names), rel
            assert not _constructs(tree, "ProviderSession"), rel
            taken = {n for n in names if n == provider or n.startswith(provider + ".")}
            assert taken <= PROVIDER_IMPORTS.get(rel, set()), rel
        if rel != "main.py":
            assert not _constructs(tree, "LiveFeed"), rel
        if any(n == feed for n in names) and rel != "hat/data/feed.py":
            assert rel in FEED_IMPORTERS, rel
    main = ast.parse((APP_ROOT / "main.py").read_text(encoding="utf-8"))
    assert _constructs(main, "LiveFeed")
    assert _guarded_by_hat_enabled(main, "_build_hat_feed")
    # the checker can fail: an unguarded call, a call under a negated guard, and one under a nested
    # `if not is_enabled()` are all refused; the plain guard passes
    for source, expected in (
        ("if is_enabled():\n    _build_hat_feed()\n", True),
        ("_build_hat_feed()\n", False),
        ("if not is_enabled():\n    _build_hat_feed()\n", False),
        ("if is_enabled():\n    if not is_enabled():\n        _build_hat_feed()\n", False),
        ("if is_enabled():\n    if ready:\n        _build_hat_feed()\n", True),
    ):
        assert _guarded_by_hat_enabled(ast.parse(source), "_build_hat_feed") is expected, source
    owners = {
        node.value
        for node in ast.walk(_hat_sources()["data/subscriptions.py"])
        if isinstance(node, ast.Constant) and node.value == "hat"
    }
    assert owners == {"hat"}
    assert "SubscriptionRegistry" in inspect.getsource(subscriptions.LiveSubscriptions)


# Modules under app/hat that may import pandas, because they adapt to an existing pandas module.
# None is left: backtest/metrics.py (the legacy metrics helper K1 moved into HAT) was rewritten
# on polars with the backtest engine in PR 12. Any future adapter would be listed here.
PANDAS_BOUNDARY_ADAPTERS: set[str] = set()


def test_imports__hat_polars_only() -> None:
    """HAT-DATA-078: no module under app/hat imports pandas (or numpy-backed pandas aliases)
    except the listed boundary adapters. Legacy pandas modules are not pinned (owner rule D59)."""
    offenders = {}
    for name, tree in _hat_sources().items():
        found = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and not node.level
        }
        if "pandas" in found and name not in PANDAS_BOUNDARY_ADAPTERS:
            offenders[name] = "pandas"
    assert offenders == {}
    assert "polars" in {
        alias.name
        for node in ast.walk(_hat_sources()["data/features.py"])
        if isinstance(node, ast.Import)
        for alias in node.names
    }
