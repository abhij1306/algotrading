"""CONF checks that HAT stands on its own: nothing under `app/hat` or `tests/hat` imports
`app.main`, a router, a test helper outside `tests/hat` or any `app` module that is not listed in
SHARED; and nothing outside HAT imports it except the database upkeep hook and the app wiring."""

import ast
import json
import subprocess
import sys
from pathlib import Path

from hat_conf_repo import APP_ROOT, REPO_ROOT

BACKEND = REPO_ROOT / "backend"
HAT_APP = APP_ROOT / "hat"
HAT_TESTS = BACKEND / "tests" / "hat"

# The `app` modules HAT may import, each with why. After K6 nothing legacy is left: these are
# shared infrastructure that holds no legacy code and no HAT-specific rule, so HAT depends on them
# without owning them. The set must equal what is imported today: a new entry is a new coupling
# (refused unless it is the same kind of module), and a module that is no longer imported must be
# removed from here.
SHARED = {
    "app.base": "the declarative base of the models",
    "app.database": "the engine, sessions and schema creation (Postgres only)",
    "app.paths": "the repository roots and `HAT_DATA_ROOT`",
    "app.utils.env_loader": "the `.env` loader and the boolean-flag parser",
}

# Top-level module names of the legacy test helpers (`tests/fakes`, the parent
# `tests/conftest.py`) and of the `tests` package itself.
LEGACY_TEST_HELPERS = {"fakes", "conftest", "tests"}

# Legacy modules that import HAT to wire it in: `database.py` (schema upkeep) and `main.py` (it
# registers HAT's routes and starts its feed). There are no re-export shims left (K5b deleted the
# last ones with the legacy features that used them).
WIRING = {"database.py", "main.py"}

# Modules a HAT suite must never load, however it reaches them: the app. (The
# manual-trading singletons they once listed are deleted, K4; `test_conf_risk.py` checks that none
# comes back.)
NEVER_LOADED = ("app.main",)


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
    """Legacy module (or test helper) -> the HAT files that import it."""
    found: dict[str, set[str]] = {}
    for root in (HAT_APP, HAT_TESTS):
        for path in sorted(root.rglob("*.py")):
            for name in _imported_modules(path):
                head = name.split(".")[0]
                legacy = (name == "app" or name.startswith("app.")) and not (
                    name == "app.hat" or name.startswith("app.hat.")
                )
                if legacy or head in LEGACY_TEST_HELPERS:
                    found.setdefault(name, set()).add(path.relative_to(BACKEND).as_posix())
    return found


def _owner(name: str) -> str | None:
    """The SHARED module that `name` is, or lives under."""
    for allowed in SHARED:
        if name == allowed or name.startswith(allowed + "."):
            return allowed
    return None


def test_imports__hat_decoupled_from_legacy() -> None:
    """: HAT code and HAT tests import no router, `app.main`, test helper outside
    `tests/hat` or any `app` module that is not listed in SHARED; and each SHARED module is still
    imported (a stale entry hides a removed coupling)."""
    imports = _legacy_imports()
    unlisted = {name: sorted(files) for name, files in imports.items() if _owner(name) is None}
    assert unlisted == {}, unlisted
    used = {_owner(name) for name in imports}
    assert used - {None} == set(SHARED)


def test_imports__legacy_does_not_import_hat() -> None:
    """: outside `app/hat`, only `app.database` (schema upkeep),
    `app.main` (wiring) import HAT. No other legacy module does: there are no re-export shims."""
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


def test_imports__hat_suites_boot_no_legacy_app() -> None:
    """: collecting every HAT suite (conftests, fakes and test modules) loads
    neither `app.main`, a router, nor the manual-trading singletons. Run in a fresh interpreter,
    so what other tests imported earlier does not hide a load."""
    code = (
        "import json, sys, pytest\n"
        "rc = pytest.main(['-q', '--no-cov', '-p', 'no:cacheprovider', '--collect-only',\n"
        "                  'tests/hat/properties', 'tests/hat/db', 'tests/hat/conformance'])\n"
        f"never = {list(NEVER_LOADED)!r}\n"
        "loaded = sorted(\n"
        "    m for m in sys.modules if any(m == n or m.startswith(n + '.') for n in never)\n"
        ")\n"
        "print('RESULT' + json.dumps({'rc': int(rc), 'loaded': loaded}))\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("RESULT")), "RESULT" + "{}"
    )
    outcome = json.loads(line[len("RESULT") :])
    assert outcome.get("rc") == 0, result.stdout[-1500:] + result.stderr[-1500:]
    assert outcome["loaded"] == [], outcome["loaded"]
