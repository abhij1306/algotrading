"""CONF checks that HAT stands on its own (K1, the cleanup plan's "Decouple HAT"): nothing under
`app/hat` or `tests/hat` imports `app.main`, a legacy router, a legacy singleton or a legacy test
helper, except the modules listed in PENDING, which later cleanup PRs move into HAT; and
nothing outside HAT imports it except the database upkeep hook."""

import ast
import json
import subprocess
import sys
from pathlib import Path

from hat_conf_repo import APP_ROOT
from hat_conf_spec import REPO_ROOT

BACKEND = REPO_ROOT / "backend"
HAT_APP = APP_ROOT / "hat"
HAT_TESTS = BACKEND / "tests" / "hat"

# Imports of the shared base that HAT keeps for good: the declarative base of the models.
PERMANENT = {"app.base"}

# Legacy modules HAT code or its tests still import, and the cleanup PR that moves them. The set
# must equal what is imported today: a new entry is a new coupling (refused), and a module that
# is no longer imported must be removed from here, so the list only shrinks.
PENDING = {
    "app.brokers.base": "K2",
    "app.brokers.plugins.fyers": "K2",
    "app.services.fyers.auth_flow": "K2",
    "app.services.fyers.token_store": "K2",
    "app.services.fyers_client": "K2",
    "app.services.symbol_master": "K2",
    "app.services.live.data_socket": "K3",
    "app.services.live.provider_session": "K3",
    "app.services.live_market_service": "K3",
    "app.services.subscription_registry": "K3",
    "app.database": "K6",
    "app.paths": "K6",
    "app.utils.env_loader": "K6",
}

# Top-level module names of the legacy test helpers (`tests/fakes`, `tests/screener_fakes.py`,
# the parent `tests/conftest.py`) and of the `tests` package itself.
LEGACY_TEST_HELPERS = {"fakes", "screener_fakes", "conftest", "tests"}

# Modules a HAT suite must never load, however it reaches them: the app, the routers and the
# singletons of the manual-trading side.
NEVER_LOADED = (
    "app.main",
    "app.routers",
    "app.services.risk_manager",
    "app.services.order_execution_service",
    "app.services.position_sync_service",
    "app.services.live_exit_service",
)


def _module_exists(name: str) -> bool:
    parts = name.split(".")
    base = BACKEND.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _imported_modules(path: Path) -> set[str]:
    """Absolute module names `path` imports; `from pkg import sub` counts as `pkg.sub` when
    `sub` is a module."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            submodules = {
                f"{node.module}.{alias.name}"
                for alias in node.names
                if _module_exists(f"{node.module}.{alias.name}")
            }
            found.update(submodules)
            if len(submodules) < len(node.names):  # a name that is not a module
                found.add(node.module)
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
    """The PENDING or PERMANENT module that `name` is, or lives under."""
    for allowed in (*PERMANENT, *PENDING):
        if name == allowed or name.startswith(allowed + "."):
            return allowed
    return None


def test_imports__hat_decoupled_from_legacy() -> None:
    """HAT-OVR-040 (K1): HAT code and HAT tests import no legacy router, `app.main`, legacy model,
    legacy test helper or any legacy module that is not listed in PENDING; and each PENDING
    module is still imported (a stale entry hides progress)."""
    imports = _legacy_imports()
    unlisted = {name: sorted(files) for name, files in imports.items() if _owner(name) is None}
    assert unlisted == {}, unlisted
    used = {_owner(name) for name in imports}
    assert used - {None} == set(PERMANENT) | set(PENDING)


def test_imports__legacy_does_not_import_hat() -> None:
    """HAT-OVR-040 (K1, risk inversion): outside `app/hat`, only `app.database` (schema upkeep)
    imports HAT. The legacy risk manager, routers and services do not."""
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel.startswith("hat/") or rel == "database.py":
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
    """HAT-OVR-040 (K1): collecting every HAT suite (conftests, fakes and test modules) loads
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
