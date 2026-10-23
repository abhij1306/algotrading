"""CONF checks of the Telegram bot's surface: the closed command set, the lever catalogue, the
transport, the text path and the owner-only auto mode."""

import ast
import importlib
import pkgutil
import re
import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter
from hat_conf_repo import APP_ROOT, build_graph, reaches
from hat_risk_support import CALENDAR
from hat_telegram_support import BOT, CHAT, OWNER, TRADE_DAY, TgRig, button_update, ist, text_update
from pydantic import BaseModel
from sqlalchemy import select

import app.hat.api
from app.hat.engine.strategy.config import StrategyConfig
from app.hat.models import HatBrokerAccount, HatModeChange
from app.hat.risk.sessions import SessionState
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
ORDER_MODULES = (
    "app.hat.execution.router",
    "app.hat.execution.submit",
    "app.hat.execution.manage",
    "app.hat.execution.protect",
    "app.hat.execution.fyers_adapter",
    "app.hat.execution.broker",
    "app.hat.engine.trades",
    "app.hat.engine.positions",
    "app.hat.engine.approvals",
    "app.hat.risk.authority",
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


def test_telegram__command_catalogue() -> None:
    """The handler table, the argument table and the command tuple are the same names; a handler
    table with an extra entry would be an unreviewed control path."""
    assert set(commands.HANDLERS) == set(commands.COMMANDS) == set(commands._ARITY)
    assert all(callable(handler) for handler in commands.HANDLERS.values())


# --- live levers only reduce risk ---------------------------------------------


def test_gov__lever_catalogue() -> None:
    """No command, button or API route sets the price, size or stop of a signal or
    exits one position. Commands take at most a bot or broker name and a mode word; the buttons
    are Take, Skip and Confirm, and Take carries only the signal (the engine sizes it); the
    only confirmable actions are flatten (all positions), arm and auto; the API writes only
    through the Fyers login routes (every other route is a read, CONF
    `hat_routes_read_only`)."""
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
        assert path.startswith("/api/hat/auth/fyers"), path
        assert not RISK_WORDS.search(path.removeprefix("/api/hat/auth/fyers")), path


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
    a request is made (the scan is shown to catch a planted listener)."""
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


def test_telegram__listener_scan_catches_a_server(tmp_path: Path) -> None:
    """The scan above finds a planted socket server and a webhook registration."""
    (tmp_path / "planted.py").write_text(
        "import socket\ns = socket.socket()\ns.bind(('0.0.0.0', 8443))\n"
        "def hook(c):\n    c.call('setWebhook', {}, 1)\n",
        encoding="utf-8",
    )
    found = _listener_findings(tmp_path)
    assert any("imports socket" in f for f in found)
    assert any("uses bind" in f for f in found)
    assert any("uses setWebhook" in f for f in found)


# --- no order from free text ---------------------------------------------------


def test_telegram__no_order_import_in_text_path() -> None:
    """No module a text message passes through reaches the router, submission, the
    trade manager, the adapter, the broker interface, trades, approvals or the risk authority,
    directly or transitively (the checker is shown to catch a planted import)."""
    graph = build_graph()
    for module in TEXT_PATH:
        assert module in graph, module
        assert reaches(graph, module, ORDER_MODULES) == [], module
    planted = {k: set(v) for k, v in graph.items()}
    planted["app.hat.telegram.reads"].add("app.hat.execution.router")
    assert reaches(planted, "app.hat.telegram.commands", ORDER_MODULES)


# --- auto is the owner's switch -----------------------------------------------


def test_auto_mode_single_user() -> None:
    """No API route sets a mode (no route module reaches the code that writes one:
    `execution.mode_changes` and the queue in `risk.controls`; the dashboard may read the mode
    through the read-only `execution.modes`),
    `/mode auto` for a bot whose account belongs to anyone but `HAT_USER_ID` is refused
    before and after its second confirmation, and the owner's own account is accepted."""
    graph = build_graph()
    mode_code = ("app.hat.execution.mode_changes", "app.hat.risk.controls")
    for info in pkgutil.iter_modules(app.hat.api.__path__, "app.hat.api."):
        assert reaches(graph, info.name, mode_code) == [], info.name
    for _path, methods in _routes():
        assert methods <= {"GET", "POST", "HEAD"}

    evening = ist(TRADE_DAY, 20)
    assert CALENDAR.state(evening) is SessionState.CLOSED
    stranger = uuid.UUID(int=OWNER.int ^ 1)
    for handed_over in (False, True):
        rig = TgRig().at(evening)
        asked = rig.service.handle(text_update(1, CHAT, f"/mode {BOT} auto"))
        assert asked.reply is not None
        assert asked.reply.accepted
        [[(_label, data)]] = asked.reply.buttons
        if handed_over:  # the account changes hands while the confirmation is pending
            _set_owner(rig, stranger)
        confirmed = rig.service.handle(button_update(2, CHAT, data))
        assert confirmed.reply is not None
        modes_queued = _modes(rig)
        if not handed_over:
            assert confirmed.reply.accepted
            assert modes_queued == ["auto"]
            continue
        assert confirmed.reply.reason == "auto_not_owner"
        assert modes_queued == []
        again = rig.service.handle(text_update(3, CHAT, f"/mode {BOT} auto"))
        assert again.reply is not None
        assert again.reply.reason == "auto_not_owner"
        assert _modes(rig) == []


def _set_owner(rig: TgRig, user_id: uuid.UUID) -> None:
    with rig.factory() as session:
        session.get_one(HatBrokerAccount, rig.account_id).user_id = user_id
        session.commit()


def _modes(rig: TgRig) -> list[str]:
    with rig.factory() as session:
        return [row.to_mode for row in session.scalars(select(HatModeChange))]
