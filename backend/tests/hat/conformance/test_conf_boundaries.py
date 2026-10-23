"""CONF checks of the module boundaries and runtime guards."""

import ast
import asyncio
import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from hat_conf_repo import APP_ROOT, REPO_ROOT, build_graph, reaches, repo_files

import app.hat

HAT_ROOT = APP_ROOT / "hat"
CRITERIA_FORBIDDEN = (
    "app.brokers",
    "app.hat.execution",
    "app.database",
    "sqlalchemy.orm",
    "app.hat.telegram",
    "fastapi",
)
BROKER_FORBIDDEN = ("app.brokers", "app.hat.execution.broker", "app.hat.execution.fyers_adapter")


def _graph_violations(
    graph: dict[str, set[str]], prefix: str, forbidden: tuple[str, ...]
) -> dict[str, list[str]]:
    return {
        module: chain
        for module in graph
        if (module == prefix or module.startswith(prefix + "."))
        and (chain := reaches(graph, module, forbidden))
    }


def test_import_graph_boundaries() -> None:
    graph = build_graph()
    assert any(m.startswith("app.hat.") for m in graph)
    criteria = [m for m in graph if m.startswith("app.hat.engine.criteria")]
    assert "app.hat.engine.criteria.vcp" in criteria
    assert len(criteria) >= 7  # not vacuous
    assert _graph_violations(graph, "app.hat.engine.criteria", CRITERIA_FORBIDDEN) == {}
    for area in ("app.hat.engine", "app.hat.data", "app.hat.risk"):
        assert _graph_violations(graph, area, BROKER_FORBIDDEN) == {}, area


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def private_imports(module: str, source: str) -> list[str]:
    """Every underscore name `module` takes from another HAT module: `from x import _name`, or
    `x._name` on a HAT module it imported (`from . import x`, `import app.hat.x as x`)."""
    package = module.rsplit(".", 1)[0]
    tree = ast.parse(source)
    modules: dict[str, str] = {}  # local name -> the HAT module it is bound to
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = package.split(".")[: len(package.split(".")) - node.level + 1]
            if node.level:
                origin = ".".join(base + ([node.module] if node.module else []))
            else:
                origin = node.module or ""
            if not origin.startswith("app.hat"):
                continue
            for alias in node.names:
                if _private(alias.name):
                    found.append(f"line {node.lineno}: from {origin} import {alias.name}")
                else:
                    modules[alias.asname or alias.name] = f"{origin}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.hat"):
                    if alias.asname:
                        modules[alias.asname] = alias.name
                    else:
                        modules["app"] = "app"  # `import app.hat.x` binds `app`
    for node in ast.walk(tree):
        owner, attr = _private_read(node)
        root, _, rest = (owner or "").partition(".")
        if root in modules:
            path = ".".join(filter(None, [modules[root], rest]))
            if path.startswith("app.hat"):
                found.append(f"line {node.lineno}: {path}.{attr}")
    return found


def _private_read(node: ast.AST) -> tuple[str | None, str]:
    """(the dotted owner, the name) of `owner._x` or `getattr(owner, "_x")`, else (None, "")."""
    if isinstance(node, ast.Attribute) and _private(node.attr):
        return _dotted(node.value), node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr", "setattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and _private(node.args[1].value)
    ):
        return _dotted(node.args[0]), node.args[1].value
    return None, ""


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of attributes on a name, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (owner := _dotted(node.value)):
        return f"{owner}.{node.attr}"
    return None


def test_no_private_imports() -> None:
    """CONF no_private_imports: no module under `app/hat`
    imports an underscore name from another HAT module or reads one off a HAT module it imported.
    A module's interface is its public names. Harm: `live_trades` imported seven private helpers
    of the shadow engine, so a refactor of shadow trading could change real-position accounting
    with nothing at the import level to say so."""
    found = {}
    for path in sorted(HAT_ROOT.rglob("*.py")):
        parts = path.relative_to(APP_ROOT.parent).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if parts[-1] == "__init__":
            module += ".__init__"  # its relative imports resolve against the package itself
        if bad := private_imports(module, path.read_text(encoding="utf-8")):
            found[str(path.relative_to(HAT_ROOT))] = bad
    assert found == {}
    # the checker can fail: a relative and an absolute private import, a private attribute of an
    # imported HAT module (by alias, by `import app.hat.x`, through a package, by `getattr`); a
    # module's own private names and a non-HAT one pass
    bad = (
        "from .trades import _emit, open_entry\n"
        "from app.hat.time import _parse\n"
        "from ..engine import trades\n"
        "import app.hat.engine.journal as journal\n"
        "from collections import _OrderedDictKeysView\n"
        "import app.hat.engine.snapshot\n"
        "from app.hat import engine\n"
        "def f(self):\n"
        "    _local(self._own)\n"
        "    app.hat.engine.snapshot._slots()\n"
        "    engine.trades._emit()\n"
        "    getattr(trades, '_emit')(getattr(trades, 'open_entry'))\n"
        "    return trades._day_bounds, journal._move, open_entry\n"
    )
    assert private_imports("app.hat.engine.live_trades", bad) == [
        "line 1: from app.hat.engine.trades import _emit",
        "line 2: from app.hat.time import _parse",
        "line 10: app.hat.engine.snapshot._slots",
        "line 11: app.hat.engine.trades._emit",
        "line 12: app.hat.engine.trades._emit",
        "line 13: app.hat.engine.trades._day_bounds",
        "line 13: app.hat.engine.journal._move",
    ]


def test_hat_package_in_backend() -> None:
    """Every module imports on this Python; no HAT file lives elsewhere."""
    names = [m.name for m in pkgutil.walk_packages(app.hat.__path__, "app.hat.")]
    assert "app.hat.ops.jobs" in names
    for name in names:
        importlib.import_module(name)
    allowed = (
        "backend/app/hat/",
        "backend/tests/hat/",
        "verification/",
    )
    stray = []
    for path in repo_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.suffix == ".py" and "hat" in {p.lower() for p in path.relative_to(REPO_ROOT).parts}:
            if not rel.startswith(allowed):
                stray.append(rel)
    assert stray == []


def test_no_deferred_integrations() -> None:
    """No HAT module imports or calls OpenAlgo; the openalgo_ref column is nullable."""
    for path in HAT_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"^\s*(import|from)\s+\S*openalgo", code, re.IGNORECASE):
                pytest.fail(f"{path}: {line}")
            assert "openalgo(" not in code.lower().replace(" ", ""), f"{path}: {line}"
    from app.hat.models.strategy import HatBrokerAccount

    assert HatBrokerAccount.__table__.columns["openalgo_ref"].nullable


def test_hat_data_root_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """The data root is env-overridable, defaults under data_system/hat, is
    gitignored, and no HAT module builds a data path any other way."""
    import app.paths as paths

    try:
        monkeypatch.setenv("HAT_DATA_ROOT", "/somewhere/else")
        importlib.reload(paths)
        assert Path("/somewhere/else") == paths.HAT_DATA_ROOT
        monkeypatch.delenv("HAT_DATA_ROOT")
        importlib.reload(paths)
        assert paths.HAT_DATA_ROOT == paths.DATA_ROOT / "hat"
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data_system/hat/" in ignore
    for path in HAT_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "data_system" not in text, path
        assert "DATA_ROOT" not in text.replace("HAT_DATA_ROOT", ""), path


def test_hat_enabled_default_false() -> None:
    from app.hat.settings import load_settings

    assert load_settings({}).enabled is False
    assert load_settings({"HAT_ENABLED": ""}).enabled is False
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "HAT_ENABLED=false" in example


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "mssql", "oracle", ""])
def test_hat_runtime_requires_postgres(dialect: str) -> None:
    """`HatRuntime.start` with HAT enabled on a non-postgresql dialect
    raises before it builds its parts (the feed, Telegram, brokers), reads the database or starts
    a task, and `app/main.py` hands it the engine's real dialect."""
    from app.hat.daily.runtime import HatRuntime
    from app.hat.errors import HatConfigError
    from app.hat.settings import DEFAULT_USER_ID, HatSettings

    settings = HatSettings(enabled=True, user_id=DEFAULT_USER_ID, data_root=Path("x"))
    touched: list[str] = []
    runtime = HatRuntime(
        settings,
        lambda: touched.append("db"),  # type: ignore[arg-type,return-value]
        dialect_name=dialect,
        parts=lambda: touched.append("parts"),  # type: ignore[arg-type,return-value]
    )

    async def start() -> int:
        with pytest.raises(HatConfigError):
            await runtime.start()
        return len(asyncio.all_tasks())

    assert asyncio.run(start()) == 1
    assert touched == []
    assert runtime.started is False
    main = ast.parse((REPO_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8"))
    dialects = [
        ast.unparse(k.value)
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "HatRuntime"
        for k in node.keywords
        if k.arg == "dialect_name"
    ]
    assert dialects == ["database.engine.dialect.name"]


SECRET_NAME = re.compile(r"^(HAT_\w*(?:TOKEN|SECRET|CHAT_ID|_URL))=(.*)$", re.MULTILINE)


def test_no_secret_values_in_repo() -> None:
    """No file holds a value for a HAT secret variable (token, chat id, approval secret, MCP
    endpoint URL), and `.env.example` lists each of them empty. The variables are the ones
    `.env.example` names."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    found = SECRET_NAME.findall(example)
    names = [name for name, _ in found]
    assert names
    assert [name for name, value in found if value.strip()] == []
    assign = re.compile(r"\b(" + "|".join(names) + r")[ \t]*[=:][ \t]*[\"']?([^\s\"'#]+)")
    offenders = []
    for path in repo_files():
        if path.suffix in {".png", ".jpg", ".ico", ".lock", ".gz", ".zip", ".woff2"} or (
            path.name == "pnpm-lock.yaml"
        ):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("backend/tests/hat/conformance/"):
            continue  # this check names the variables without values
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in assign.finditer(text):
            offenders.append((rel, match.group(1), match.group(2)))
    # os.environ lookups and the like are not literals
    assert [o for o in offenders if not o[2].startswith(("os.", "env", "source", "settings"))] == []
