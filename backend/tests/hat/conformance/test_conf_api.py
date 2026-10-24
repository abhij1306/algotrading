"""CONF check of HAT's HTTP surface: the dashboard is read-only and Telegram is the
control surface. Every route of every router under `app/hat/api` is a GET, except the Fyers login
routes listed in WRITE_ROUTES, and no GET route reaches a database write.

The app itself is never built here (`app.main` is out of bounds for `tests/hat`): the check reads
the routers of the `app.hat.api` modules and the source of each GET endpoint. From an endpoint it
follows every call it can resolve to a function of an `app.hat` module (the module's own helpers,
`read_bots.*` and so on) and refuses a session write (`add`, `commit`,
`delete`, `flush`, ...), an `insert`/`update`/`delete` statement or an event `record`. The shared
read model (`app/hat/read`, which the dashboard and Telegram both use) is held to the same rule:
no function of it writes and it imports no module that does. Every
write route depends on the write guard (`app.hat.api.guard`); the app
wiring hands the guard the CORS frontend-origin rule; the server binds to loopback by default. The
wiring is read from the source of `app/main.py` and `start_server.py`, which are never imported.
"""

import ast
import importlib
import inspect
import pkgutil
import textwrap
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute
from hat_conf_repo import REPO_ROOT, build_graph, reaches

import app.hat.api
import app.hat.read
from app.hat.api import guard

# The only routes that are not GET: the daily Fyers login. It writes the token
# file, never a HAT table, and each route depends on the write guard.
WRITE_ROUTES = {
    ("POST", "/api/hat/auth/fyers/token"),
    ("POST", "/api/hat/auth/fyers/disconnect"),
    ("POST", "/api/hat/auth/zerodha/token"),
    ("POST", "/api/hat/auth/zerodha/disconnect"),
}
READ_METHODS = {"GET", "HEAD"}
# Session methods that write, and statement builders and helpers that write.
WRITE_ATTRS = {
    "add",
    "add_all",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
    "commit",
    "delete",
    "flush",
    "merge",
}
WRITE_CALLS = {"insert", "update", "delete", "record"}
HAT_PACKAGE = "app.hat."
# The modules that write a HAT table or send something; the read model reaches none of them.
WRITER_MODULES = (
    "app.hat.events",
    "app.hat.risk.controls",
    "app.hat.risk.kill_ops",
    "app.hat.risk.drawdown",
    "app.hat.risk.bots",
    "app.hat.execution.mode_changes",
    "app.hat.telegram",
)


def _routers() -> Iterator[tuple[str, APIRouter]]:
    for info in pkgutil.iter_modules(app.hat.api.__path__, "app.hat.api."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                yield info.name, value


def _routes() -> list[tuple[str, APIRoute]]:
    """(module, route) of every route of every router under app.hat.api."""
    found = []
    for module, router in _routers():
        for route in router.routes:
            assert isinstance(route, APIRoute), (module, route)
            found.append((module, route))
    return found


def _function_node(func: Callable[..., Any]) -> ast.AST | None:
    """The parsed definition of `func`, or None when it has no Python source."""
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return None
    return ast.parse(source)


def _resolve(node: ast.expr, scope: dict[str, Any]) -> object | None:
    """The object a call target names in the function's module, when it can be known."""
    if isinstance(node, ast.Name):
        return scope.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = scope.get(node.value.id)
        if isinstance(owner, ModuleType):
            return getattr(owner, node.attr, None)
    return None


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_hat_function(target: object) -> bool:
    module = getattr(target, "__module__", "") or ""
    return inspect.isfunction(target) and module.startswith(HAT_PACKAGE)


def _writes_in(func: Callable[..., Any], seen: set[Callable[..., Any]]) -> list[str]:
    """Every write `func` performs, itself or through a HAT function it calls (transitively)."""
    if func in seen:
        return []
    seen.add(func)
    tree = _function_node(func)
    if tree is None:
        return []
    scope = vars(inspect.getmodule(func) or app.hat.api)
    where = f"{func.__module__}.{func.__qualname__}"
    found = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = _call_name(call.func)
        if isinstance(call.func, ast.Attribute) and name in WRITE_ATTRS:
            found.append(f"{where}: .{name}()")
        elif isinstance(call.func, ast.Name) and name in WRITE_CALLS:
            found.append(f"{where}: {name}()")
        target = _resolve(call.func, scope)
        if _is_hat_function(target):
            found.extend(_writes_in(target, seen))  # type: ignore[arg-type]
    return found


def _read_model_functions() -> Iterator[Callable[..., Any]]:
    for info in pkgutil.iter_modules(app.hat.read.__path__, "app.hat.read."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if inspect.isfunction(value) and value.__module__ == info.name:
                yield value


def test_hat_routes_read_only() -> None:
    """Every HAT route is a read (GET) except the listed Fyers login routes, the
    listed exemptions all exist, and no GET endpoint reaches a session write, a write statement
    or an event record. The shared read model writes nothing and imports no writing module.
    Harm: the dashboard becomes a second control surface that bypasses Telegram's
    confirmations and the risk authority."""
    functions = list(_read_model_functions())
    assert functions
    for func in functions:
        assert _writes_in(func, set()) == [], func.__qualname__
    graph = build_graph()
    for info in pkgutil.iter_modules(app.hat.read.__path__, "app.hat.read."):
        assert reaches(graph, info.name, WRITER_MODULES) == [], info.name
    routes = _routes()
    assert routes
    writes = set()
    for module, route in routes:
        assert route.path.startswith("/api/hat/"), (module, route.path)
        methods = set(route.methods or ())
        assert methods, (module, route.path)
        if methods <= READ_METHODS:
            assert _writes_in(route.endpoint, set()) == [], (module, route.path)
        else:
            writes |= {(method, route.path) for method in methods}
    assert writes == WRITE_ROUTES


def _dependency_calls(route: APIRoute) -> set[object]:
    """Every dependency callable of the route, nested ones included."""
    found: set[object] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        found.add(dependant.call)
        pending.extend(dependant.dependencies)
    return found


def test_hat_writes_guarded() -> None:
    """Every route under `app/hat/api` that is not a read depends on
    `guard.frontend_write`, so no write runs before the guard has refused a request from another
    site. Harm: a new POST route ships open to cross-site requests (CSRF) on the owner's browser."""
    writes = [
        (module, route)
        for module, route in _routes()
        if not set(route.methods or ()) <= READ_METHODS
    ]
    assert {(m, r.path) for m, r in writes}
    for module, route in writes:
        assert guard.frontend_write in _dependency_calls(route), (module, route.path)


def _assigned(tree: ast.AST, target: str) -> list[ast.expr]:
    """The values assigned to the dotted name `target` anywhere in `tree`."""
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(ast.unparse(t) == target for t in node.targets)
    ]


def test_api_loopback_no_auth_wiring() -> None:
    """The API has no auth and relies on its bind and the write guard, so
    `start_server.py` binds `BACKEND_HOST` to 127.0.0.1 by default, and `app/main.py` sets
    `app.state.frontend_origin` to the CORS rule `is_frontend_origin` imported from
    `app.utils.origins`. Harm: the server listens on every interface with no auth, or the guard
    has no rule and either refuses the HAT page or is replaced by a looser rule than CORS."""
    start = ast.parse((REPO_ROOT / "backend" / "start_server.py").read_text(encoding="utf-8"))
    defaults = [
        node.args[1]
        for node in ast.walk(start)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "os.getenv"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "BACKEND_HOST"
    ]
    assert [ast.unparse(value) for value in defaults] == ["'127.0.0.1'"]

    main = ast.parse((REPO_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8"))
    wired = _assigned(main, f"app.state.{guard.ORIGIN_RULE}")
    assert [ast.unparse(value) for value in wired] == ["is_frontend_origin"]
    imported = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.ImportFrom)
        and node.module == "utils.origins"
        and node.level == 1
        and any(alias.name == "is_frontend_origin" and alias.asname is None for alias in node.names)
    ]
    assert len(imported) == 1


def test_frontend_sends_write_header() -> None:
    """The frontend API client names the guard's header and value, and sets them in
    its one write middleware. Harm: a rename on either side makes every login and logout from
    the HAT page a 403."""
    client = (REPO_ROOT / "frontend" / "lib" / "api" / "client.ts").read_text(encoding="utf-8")
    assert f'export const WRITE_HEADER = "{guard.HEADER}";' in client
    assert f'request.headers.set(WRITE_HEADER, "{guard.HEADER_VALUE}")' in client
    assert client.count("client.use(") == 1
