"""CONF check of HAT's HTTP surface (HAT-DEC-T3): the dashboard is read-only and Telegram is the
control surface. Every route of every router under `app/hat/api` is a GET, except the Fyers login
routes listed in WRITE_ROUTES, and no GET route reaches a database write.

The app itself is never built here (`app.main` is out of bounds for `tests/hat`): the check reads
the routers of the `app.hat.api` modules and the source of each GET endpoint. From an endpoint it
follows every call it can resolve to a function of an `app.hat` module (the module's own helpers,
`reads.*`, `controls.kill_state` and so on) and refuses a session write (`add`, `commit`,
`delete`, `flush`, ...), an `insert`/`update`/`delete` statement or an event `record`.
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

import app.hat.api

# The only routes that are not GET: the daily Fyers login (HAT-OPS-051, K2). It writes the token
# file, never a HAT table.
WRITE_ROUTES = {
    ("POST", "/api/hat/auth/fyers/token"),
    ("POST", "/api/hat/auth/fyers/disconnect"),
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


def test_hat_routes_read_only() -> None:
    """HAT-DEC-T3: every HAT route is a read (GET) except the listed Fyers login routes, the
    listed exemptions all exist, and no GET endpoint reaches a session write, a write statement
    or an event record. Harm: the dashboard becomes a second control surface that bypasses
    Telegram's confirmations and the risk authority."""
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
