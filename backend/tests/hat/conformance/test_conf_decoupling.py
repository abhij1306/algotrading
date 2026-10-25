"""CONF checks that HAT stands on its own: nothing under `app/hat` or `tests/hat` imports
`app.main`, a router or any `app` module that is not listed in SHARED; and nothing outside HAT
imports it except the database upkeep hook and the app wiring."""

import ast
from pathlib import Path

from hat_conf_repo import APP_ROOT, REPO_ROOT

BACKEND = REPO_ROOT / "backend"
HAT_APP = APP_ROOT / "hat"
HAT_TESTS = BACKEND / "tests" / "hat"

# The `app` modules HAT may import, each with why: shared infrastructure that holds no
# HAT-specific rule, so HAT depends on them without owning them. The set must equal what is
# imported today: a new entry is a new coupling (refused unless it is the same kind of module), and
# a module that is no longer imported must be removed from here.
SHARED = {
    "app.base": "the declarative base of the models",
    "app.database": "the engine, sessions and schema creation (Postgres only)",
    "app.paths": "the repository roots and `HAT_DATA_ROOT`",
    "app.utils.env_loader": "the `.env` loader and the boolean-flag parser",
}

# The `app` modules that import HAT to wire it in: `database.py` (schema upkeep) and `main.py` (it
# registers HAT's routes and starts its feed).
WIRING = {"database.py", "main.py"}


def _module_exists(name: str) -> bool:
    parts = name.split(".")
    base = BACKEND.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _absolute(path: Path, node: ast.ImportFrom) -> str:
    """The absolute module an `ImportFrom` names, resolving a relative import against `path`."""
    if node.level == 0:
        return node.module or ""
    package = path.relative_to(BACKEND).with_suffix("").parts[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _imported_modules(path: Path) -> set[str]:
    """Absolute module names `path` imports; `from pkg import sub` counts as `pkg.sub` when
    `sub` is a module."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
            module = _absolute(path, node)
            submodules = {
                f"{module}.{alias.name}"
                for alias in node.names
                if _module_exists(f"{module}.{alias.name}")
            }
            found.update(submodules)
            if len(submodules) < len(node.names):  # a name that is not a module
                found.add(module)
    return found


def _legacy_imports() -> dict[str, set[str]]:
    """Non-HAT `app` module -> the HAT files that import it."""
    found: dict[str, set[str]] = {}
    for root in (HAT_APP, HAT_TESTS):
        for path in sorted(root.rglob("*.py")):
            for name in _imported_modules(path):
                legacy = (name == "app" or name.startswith("app.")) and not (
                    name == "app.hat" or name.startswith("app.hat.")
                )
                if legacy:
                    found.setdefault(name, set()).add(path.relative_to(BACKEND).as_posix())
    return found


def _owner(name: str) -> str | None:
    """The SHARED module that `name` is, or lives under."""
    for allowed in SHARED:
        if name == allowed or name.startswith(allowed + "."):
            return allowed
    return None


def test_imports__hat_decoupled_from_legacy() -> None:
    """HAT code and HAT tests import no router, `app.main` or any `app` module that is not listed
    in SHARED; and each SHARED module is still imported (a stale entry hides a removed coupling)."""
    imports = _legacy_imports()
    unlisted = {name: sorted(files) for name, files in imports.items() if _owner(name) is None}
    assert unlisted == {}, unlisted
    used = {_owner(name) for name in imports}
    assert used - {None} == set(SHARED)


def test_imports__legacy_does_not_import_hat() -> None:
    """Outside `app/hat`, only `app.database` (schema upkeep) and `app.main` (wiring) import HAT:
    there are no re-export shims."""
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel.startswith("hat/") or rel in WIRING:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [("." * node.level) + (node.module or "")] + [
                    alias.name for alias in node.names
                ]
            if any(n.lstrip(".").split(".")[0] == "hat" or n.startswith("app.hat") for n in names):
                offenders.append(rel)
    assert offenders == []
