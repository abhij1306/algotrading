"""CONF checks of the Telegram bot's surface: the closed command set, the lever catalogue, the
transport, the text path and the owner-only auto mode."""

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from fastapi import APIRouter
from hat_conf_repo import APP_ROOT, build_graph, reaches
from pydantic import BaseModel

import app.hat.api
from app.hat.engine.strategy.config import StrategyConfig
from app.hat.telegram import callbacks, commands, tokens
from app.hat.telegram.client import ALLOWED_METHODS, CODE_METHOD, HttpsTransport, TelegramError

TELEGRAM = APP_ROOT / "hat" / "telegram"
# The modules a text message passes through (service.py hands buttons to callbacks.py).
TEXT_PATH = (
    "app.hat.telegram.commands",
    "app.hat.telegram.reads",
    "app.hat.telegram.render",
    "app.hat.telegram.confirm",
    "app.hat.telegram.store",
    "app.hat.telegram.client",
)
# The only writing routes: the logins, and the backtest job start (a research run in a child
# process, which touches no live position, signal or order).
WRITE_BASES = ("/api/hat/auth/fyers/", "/api/hat/auth/zerodha/", "/api/hat/backtests")
# The only execution modules the text path may reach: the mode gates `/mode` reads and the queue
# `/auto` writes. Every other execution module (orders, brokers, stops, reconciliation) is
# forbidden, so a new one is forbidden by default.
TEXT_PATH_EXECUTION = frozenset({"app.hat.execution.modes", "app.hat.execution.mode_changes"})
# Outside execution: the risk authority and the engine code that opens, sizes or approves a trade.
ORDER_ENGINE = (
    "app.hat.risk.service",
    "app.hat.risk.decision",
    "app.hat.engine.entries",
    "app.hat.engine.shadow",
    "app.hat.engine.positions",
    "app.hat.engine.approvals",
)
LISTENER_IMPORTS = ("socket", "socketserver", "http.server", "aiohttp", "uvicorn", "fastapi")
LISTENER_CALLS = {"bind", "listen", "serve_forever", "start_server", "setWebhook"}
# A lever that would set a price, a size or a stop, exit one position, or force a gate on.
RISK_WORDS = re.compile(
    r"buy|sell|order|price|qty|quantity|size|stop|target|exit|close|override|force|gate|regime"
)


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def _calls(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.update(re.findall(r"\b(setWebhook)\b", node.value))
    return names


def _listener_findings(root: Path) -> list[str]:
    found = []
    for path in _sources(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imports(tree):
            if name.split(".")[0] in LISTENER_IMPORTS or name in LISTENER_IMPORTS:
                found.append(f"{path.name} imports {name}")
        for call in _calls(tree) & LISTENER_CALLS:
            found.append(f"{path.name} uses {call}")
    return found


READS = {"GET", "HEAD"}


def _routes() -> list[tuple[str, set[str]]]:
    """(path, methods) of every route of every router under app.hat.api. A router's routes
    already carry its prefix."""
    routes = []
    for info in pkgutil.iter_modules(app.hat.api.__path__, "app.hat.api."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                for route in value.routes:
                    routes.append((route.path, set(route.methods or ())))
    return routes


def _field_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for arg in [field.annotation, *getattr(field.annotation, "__args__", ())]:
            for inner in [arg, *getattr(arg, "__args__", ())]:
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    names |= _field_names(inner, seen)
    return names


# --- the command set ----------------------------------------------


def test_telegram_parser_accepts_only_its_commands() -> None:
    """Every accepted command parses and the parser refuses a trading or unknown command."""
    for command in commands.COMMANDS:
        assert commands.parse(command if command != "/mode" else "/mode trend shadow") is not None
    for stray in ("/buy", "/sell", "/override", "/exit", "/start", "/help"):
        assert commands.parse(stray) is None, stray


# --- live levers only reduce risk ---------------------------------------------


def test_gov__lever_catalogue() -> None:
    """No command, button or API route sets the price, size or stop of a signal or
    exits one position. Commands take at most a bot or broker name and a mode word; the buttons
    are Take, Skip and Confirm, and Take carries only the signal (the engine sizes it); the
    only confirmable actions are flatten (all positions), arm and auto; the API writes only
    through the broker login routes and the backtest job start (every other route is a read,
    CONF `hat_routes_read_only`)."""
    for command, (_fewest, most) in commands._ARITY.items():
        assert not RISK_WORDS.search(command[1:]), command
        assert most <= 2, command
    assert set(tokens.ACTIONS) | {tokens.CONFIRM} == {"t", "s", "c"}
    for protocol in (callbacks.ApprovalDesk.take, callbacks.ApprovalDesk.skip):
        params = set(protocol.__code__.co_varnames[: protocol.__code__.co_argcount])
        params |= set(protocol.__kwdefaults__ or {}) | set(protocol.__annotations__) - {"return"}
        assert params <= {"self", "session", "signal_id", "now", "chat_id"}, params
    flatten = callbacks.Flattener.flatten
    assert set(flatten.__annotations__) - {"return"} == {"session", "now"}
    asked = set(re.findall(r'_ask\(ctx, "(\w+)"', (TELEGRAM / "commands.py").read_text("utf-8")))
    assert asked == {"flatten", "arm", "mode_auto"}
    source = (TELEGRAM / "callbacks.py").read_text(encoding="utf-8")
    assert set(re.findall(r'pending\.action == "(\w+)"', source)) == {"flatten", "arm"}
    for path, methods in _routes():
        if methods <= READS:
            continue
        base = next((b for b in WRITE_BASES if path.startswith(b)), None)
        assert base is not None, path
        assert not RISK_WORDS.search(path.removeprefix(base)), path


def test_no_override_command() -> None:
    """No Telegram command, API route or strategy config field can set the regime
    gate on: no command or writing (non-GET) route names a gate, regime or override, no config
    field is an override, and nothing in the Telegram or API packages writes the regime state."""
    for command in commands.COMMANDS:
        assert not re.search(r"gate|regime|override|force", command), command
    for path, methods in _routes():
        assert methods <= READS or not re.search(r"gate|regime|override|force", path), path
    for name in _field_names(StrategyConfig):
        assert not re.search(r"override|force|bypass", name), name
    for root in (TELEGRAM, APP_ROOT / "hat" / "api"):
        for path in _sources(root):
            text = path.read_text(encoding="utf-8")
            assert "HatRegimeState(" not in text, path.name
            assert "regime_state" not in text, path.name


# --- long polling only ---------------------------------------------------------


def test_telegram__no_webhook_no_listener() -> None:
    """Nothing in app/hat/telegram imports a server stack, binds, listens or names
    setWebhook; the client allows only the five outbound methods and refuses any other before
    a request is made."""
    assert _listener_findings(TELEGRAM) == []
    assert (
        frozenset(
            {"getMe", "getUpdates", "sendMessage", "answerCallbackQuery", "editMessageReplyMarkup"}
        )
        == ALLOWED_METHODS
    )

    posted: list[str] = []
    transport = HttpsTransport("unused", post=lambda url, **_kw: posted.append(url))
    for method in ("setWebhook", "deleteWebhook", "close", "logOut"):
        with pytest.raises(TelegramError) as raised:
            transport.call(method, {}, 1)
        assert raised.value.code == CODE_METHOD
    assert posted == []


# --- no order from free text ---------------------------------------------------


def order_modules(graph: dict[str, set[str]]) -> tuple[str, ...]:
    """Every execution module but the text path's two, plus `ORDER_ENGINE`."""
    execution = {m for m in graph if m.startswith("app.hat.execution.")} - TEXT_PATH_EXECUTION
    return (*sorted(execution), *ORDER_ENGINE)


def test_telegram__no_order_import_in_text_path() -> None:
    """No module a text message passes through reaches an order, broker or stop module, the
    risk authority, entries, trades or approvals, directly or transitively. Every named module
    exists, so a rename cannot empty the check."""
    graph = build_graph()
    forbidden = order_modules(graph)
    for module in (*ORDER_ENGINE, *TEXT_PATH_EXECUTION):
        assert module in graph, module
    assert {"app.hat.execution.submit", "app.hat.execution.fyers_broker"} <= set(forbidden)
    for module in TEXT_PATH:
        assert module in graph, module
        assert reaches(graph, module, forbidden) == [], module


# --- auto is the owner's switch -----------------------------------------------


def test_no_route_sets_a_mode() -> None:
    """No API route sets a mode (no route module reaches the code that writes one:
    `execution.mode_changes` and the queue in `risk.controls`; the dashboard may read the mode
    through the read-only `execution.modes`). Who may set auto on Telegram is
    `commands.auto_refusal` (tested in `properties/test_telegram.py`)."""
    graph = build_graph()
    mode_code = ("app.hat.execution.mode_changes", "app.hat.risk.controls")
    for info in pkgutil.iter_modules(app.hat.api.__path__, "app.hat.api."):
        assert reaches(graph, info.name, mode_code) == [], info.name
    for _path, methods in _routes():
        assert methods <= {"GET", "POST", "HEAD"}
