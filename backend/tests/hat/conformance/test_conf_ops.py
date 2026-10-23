"""CONF checks of the deployment: the Compose file brings up only Postgres and the backend
(the frontend under a profile) on loopback ports with no inline secret, and the owner's admin
command line only calls service functions."""

import ast
import re

import yaml
from hat_conf_repo import APP_ROOT, REPO_ROOT, build_graph

COMPOSE = REPO_ROOT / "deploy" / "hat" / "docker-compose.yml"
ENV_FILE = "../../.env"
CORE_SERVICES = {"postgres", "backend"}
OPTIONAL_SERVICES = {"frontend"}
LOOPBACK = "127.0.0.1"
POSTGRES_MAJOR = "18"
SECRET_KEY = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|CHAT_ID", re.IGNORECASE)
INTERPOLATED = re.compile(r"^\$\{[A-Z_]+(:?[?-][^}]*)?\}$")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _host_ip(port: object) -> str:
    """The host address of a published port, short (`ip:host:container`) or long syntax."""
    if isinstance(port, dict):
        return str(port.get("host_ip", ""))
    parts = str(port).split(":")
    return parts[0] if len(parts) == 3 else ""


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.partition("=")[::2] for item in env)
    return {str(key): str(value) for key, value in env.items()}


def test_compose_services() -> None:
    """Exactly postgres and backend (frontend only under a profile), no other
    service, Postgres 18, every published port on 127.0.0.1, the backend reading the repo .env
    through env_file, no secret written inline, and the backend starting HAT after a healthy
    database.
    Harm: an open port exposes the trading box, or a stray service holds broker credentials."""
    services = _compose()["services"]
    assert set(services) - OPTIONAL_SERVICES == CORE_SERVICES
    for name in CORE_SERVICES:
        assert "profiles" not in services[name], name
    for name in set(services) & OPTIONAL_SERVICES:
        assert services[name].get("profiles"), name
    image = services["postgres"]["image"]
    assert image.split(":")[0] == "postgres"
    assert image.split(":")[1].split(".")[0] == POSTGRES_MAJOR
    for name, service in services.items():
        for port in service.get("ports", []):
            assert _host_ip(port) == LOOPBACK, (name, port)
        for key, value in _environment(service).items():
            if SECRET_KEY.search(key):
                assert INTERPOLATED.match(value), (name, key)
    assert ENV_FILE in services["backend"]["env_file"]
    _check_backend(services["backend"])


def _check_backend(backend: dict) -> None:
    env = _environment(backend)
    assert env["HAT_ENABLED"] == "true"
    assert env["DB_HOST"] == "postgres"
    assert backend["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert backend["healthcheck"]["test"]
    mounts = {str(volume).split(":")[1] for volume in backend.get("volumes", [])}
    assert env["HAT_DATA_ROOT"] in mounts


ADMIN = APP_ROOT / "hat" / "admin.py"
# The modules the admin command line may import: the services it wraps and what they need.
ADMIN_IMPORTS = {
    "__future__",
    "argparse",
    "collections.abc",
    "contextlib",
    "dataclasses",
    "datetime",
    "decimal",
    "json",
    "pathlib",
    "sys",
    "typing",
    "uuid",
    "sqlalchemy.exc",
    "sqlalchemy.orm",
    "app.database",
    "app.hat.clock",  # the Clock seam: reads no table (PR 75e)
    "app.hat.backtest.runs",
    "app.hat.data.calendar",
    "app.hat.data.reference",
    "app.hat.engine.portfolio",
    "app.hat.engine.owner",
    "app.hat.engine.portfolio.common",
    "app.hat.engine.portfolio.sleeve_service",
    "app.hat.engine.portfolio.universe_service",
    "app.hat.engine.rescale",
    "app.hat.engine.strategy",
    "app.hat.engine.strategy.config",
    "app.hat.engine.strategy.yamlio",
    "app.hat.engine.trades",
    "app.hat.errors",
    "app.hat.execution.accounts",
    "app.hat.execution.broker",
    "app.hat.money",  # the Decimal boundary: reads no table (PR 75c)
    "app.hat.risk.policy",
    "app.hat.risk.policy_service",
    "app.hat.risk.sessions",
    "app.hat.settings",
    "app.hat.telegram.config",
    "app.hat.time",
}
QUERY_NAMES = {"select", "insert", "update", "delete", "text", "func"}


def _absolute(node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = "app.hat"  # admin.py sits in app/hat
    for _ in range(node.level - 1):
        package = package.rsplit(".", 1)[0]
    return f"{package}.{node.module}" if node.module else package


def admin_violations(source: str) -> list[str]:
    """What breaks the rule in the admin module's source: an import outside ADMIN_IMPORTS, a
    method call on a session (a query or a write), a query builder, or an assignment to an
    attribute other than the module's own error's (a column written here instead of in a
    service)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name not in ADMIN_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node)
            if module not in ADMIN_IMPORTS:
                found.append(f"from {module}")
            found += [f"import {a.name}" for a in node.names if a.name in QUERY_NAMES]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "session":
                found.append(f"session.{node.func.attr}()")
        elif isinstance(node, ast.Name) and node.id in QUERY_NAMES:
            found.append(node.id)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [
                f"assigns .{t.attr}"
                for t in targets
                if isinstance(t, ast.Attribute)
                and not (isinstance(t.value, ast.Name) and t.value.id == "self")
            ]
    return found


def test_admin_cli_calls_services() -> None:
    """CONF ops.admin_cli_calls_services: the admin command line reads
    no table and writes no column itself (every step is a guarded service function), and no
    route or app module imports it, so it is never reachable over HTTP. Harm: a setup command
    skips the guard a service enforces, or the owner's commands become an unauthenticated API."""
    assert admin_violations(ADMIN.read_text("utf-8")) == []
    graph = build_graph()
    assert "app.hat.admin" in graph
    importers = sorted(m for m, deps in graph.items() if "app.hat.admin" in deps)
    assert importers == []


def unalerted_subscriptions(source: str) -> list[int]:
    """The lines of every `LiveSubscriptions(...)` in a module's source (by its name, an import
    alias or `module.LiveSubscriptions`) built without its `alert` (the fourth positional
    argument or the keyword), or with `None` for it."""
    tree = ast.parse(source)
    names = {"LiveSubscriptions"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "LiveSubscriptions" and alias.asname
    }

    def builds(func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in names
        return isinstance(func, ast.Attribute) and func.attr == "LiveSubscriptions"

    def alert(call: ast.Call) -> ast.expr | None:
        if len(call.args) >= 4:
            return call.args[3]
        return next((k.value for k in call.keywords if k.arg == "alert"), None)

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and builds(node.func)
        and (
            (given := alert(node)) is None
            or (isinstance(given, ast.Constant) and given.value is None)
        )
    ]


def test_ops__subscription_alert_wired() -> None:
    """CONF ops.subscription_alert_wired: every `LiveSubscriptions`
    the app builds is given its alert, and the runtime builds one. Harm: `subscription_limit`
    reaches only the log, so the owner never hears that symbols past the provider's limit get no
    ticks."""
    sources = {
        str(path.relative_to(APP_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted((APP_ROOT / "hat").rglob("*.py"))
    }
    found = {
        name: lines for name, text in sources.items() if (lines := unalerted_subscriptions(text))
    }
    assert found == {}
    assert "LiveSubscriptions(" in sources["hat/daily/assembly.py"]
    # the checker can fail: built without its alert, with None, through an alias or the module;
    # one with it by keyword passes
    assert unalerted_subscriptions(
        "from x import LiveSubscriptions as Subs\n"
        "LiveSubscriptions(r, s, u)\n"
        "LiveSubscriptions(r, s, u, alert=a)\n"
        "LiveSubscriptions(r, s, u, alert=None)\n"
        "Subs(r, s, u)\n"
        "subscriptions.LiveSubscriptions(r, s, u, None)\n"
    ) == [2, 4, 5, 6]
