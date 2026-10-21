"""CONF checks of the module boundaries and runtime guards (HAT-OVR-003/004/005/007/052,
HAT-DEC-T2/T5/T13/BD1/BD3, HAT-SCH-021/105/106, HAT-OPS-050)."""

import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from hat_conf_repo import APP_ROOT, build_graph, reaches, repo_files
from hat_conf_spec import REPO_ROOT, SPEC_DIR
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.pool import StaticPool

import app.hat

HAT_ROOT = APP_ROOT / "hat"
CRITERIA_FORBIDDEN = (
    "app.brokers",
    "app.hat.execution",
    "app.database",
    "sqlalchemy.orm",
    "app.routers",
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
    assert "app.hat.engine.criteria.vcp" in criteria and len(criteria) >= 7  # not vacuous
    assert _graph_violations(graph, "app.hat.engine.criteria", CRITERIA_FORBIDDEN) == {}
    for area in ("app.hat.engine", "app.hat.data", "app.hat.risk"):
        assert _graph_violations(graph, area, BROKER_FORBIDDEN) == {}, area


def test_import_graph_checker_can_fail() -> None:
    """The checker finds a direct and a transitive violation in a synthetic graph."""
    graph = {
        "app.hat.engine.criteria.a": {"app.hat.engine.criteria.helper"},
        "app.hat.engine.criteria.helper": {"sqlalchemy.orm"},
        "app.hat.risk.sizing": {"app.brokers.plugins.fyers"},
        "app.hat.data.ok": {"json"},
    }
    found = _graph_violations(graph, "app.hat.engine.criteria", CRITERIA_FORBIDDEN)
    assert found["app.hat.engine.criteria.a"] == [
        "app.hat.engine.criteria.a",
        "app.hat.engine.criteria.helper",
        "sqlalchemy.orm",
    ]
    assert _graph_violations(graph, "app.hat.risk", BROKER_FORBIDDEN)
    assert _graph_violations(graph, "app.hat.data", BROKER_FORBIDDEN) == {}


def test_hat_package_in_backend() -> None:
    """HAT-DEC-T2/BD1: every module imports on this Python; no HAT file lives elsewhere."""
    names = [m.name for m in pkgutil.walk_packages(app.hat.__path__, "app.hat.")]
    assert "app.hat.ops.jobs" in names
    for name in names:
        importlib.import_module(name)
    allowed = (
        "backend/app/hat/",
        "backend/tests/hat/",
        "verification/",
        "docs/specs/hat/",
        ".claude/skills/verify-",
    )
    stray = []
    for path in repo_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.suffix == ".py" and "hat" in {p.lower() for p in path.relative_to(REPO_ROOT).parts}:
            if not rel.startswith(allowed):
                stray.append(rel)
    assert stray == []


def test_no_deferred_integrations() -> None:
    """HAT-DEC-BD3: no HAT module imports or calls OpenAlgo; the openalgo_ref column is nullable."""
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
    """HAT-OVR-052: the data root is env-overridable, defaults under data_system/hat, is
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
def test_runtime_refuses_sqlite(dialect: str) -> None:
    """HAT-SCH-105 / DEC-T5: a non-postgresql dialect raises before logging, recovery or a task."""
    from app.hat.errors import HatConfigError
    from app.hat.ops import scheduler
    from app.hat.settings import DEFAULT_USER_ID, HatSettings

    settings = HatSettings(enabled=True, user_id=DEFAULT_USER_ID, data_root=Path("x"))
    called = []
    original = scheduler.HatScheduler
    scheduler.HatScheduler = lambda *a, **k: called.append(1)  # type: ignore[assignment]
    try:
        with pytest.raises(HatConfigError):
            scheduler.start_scheduler(settings, dialect_name=dialect)
    finally:
        scheduler.HatScheduler = original
    assert called == []


def test_runtime_requires_postgres() -> None:
    test_runtime_refuses_sqlite("sqlite")


def _memory_sqlite() -> Engine:
    return create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )


def test_seed_market_nse() -> None:
    """HAT-SCH-021: after any number of seeds exactly one NSE row with the stated values; and
    ensure_hat_schema issues no DDL on SQLite."""
    from sqlalchemy.orm import Session

    import app.hat.models  # noqa: F401
    from app.base import Base
    from app.hat.models.reference import HatMarket
    from app.hat.schema import ensure_hat_schema, seed_hat_reference

    engine = _memory_sqlite()
    Base.metadata.create_all(engine)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
    for _ in range(3):
        ensure_hat_schema(engine)
        seed_hat_reference(engine)
    assert not any(re.match(r"\s*(CREATE|ALTER|DROP)", s, re.IGNORECASE) for s in statements)
    with Session(engine) as session:
        rows = session.scalars(select(HatMarket)).all()
    assert [r.code for r in rows] == ["NSE"]
    nse = rows[0]
    assert (nse.timezone, nse.currency) == ("Asia/Kolkata", "INR")
    assert (nse.session_open.isoformat(), nse.session_close.isoformat()) == ("09:15:00", "15:30:00")


def _secret_variables() -> list[str]:
    text = (SPEC_DIR / "10-ops.md").read_text(encoding="utf-8")
    table = text[text.index("`HAT_TELEGRAM_TOKEN`") - 200 : text.index("#### HAT-OPS-050")]
    return re.findall(r"^\| `(HAT_[A-Z_]+)`", table, re.MULTILINE)


def test_ops_no_secrets_in_repo() -> None:
    """HAT-OPS-050 / DEC-T13: no file holds a value for a HAT variable and .env.example lists
    each of them with an empty value."""
    names = _secret_variables()
    assert set(names) >= {
        "HAT_TELEGRAM_TOKEN",
        "HAT_TELEGRAM_CHAT_ID",
        "HAT_APPROVAL_SECRET",
        "HAT_MCP_BHAVCOPY_URL",
        "HAT_MCP_CMMARKET_URL",
    }
    assign = re.compile(r"\b(" + "|".join(names) + r")[ \t]*[=:][ \t]*[\"']?([^\s\"'#]+)")
    offenders = []
    for path in repo_files():
        if path.suffix in {".png", ".jpg", ".ico", ".lock", ".gz", ".zip", ".woff2"} or (
            path.name == "pnpm-lock.yaml"
        ):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("docs/specs/hat/") or rel.startswith("backend/tests/hat/conformance/"):
            continue  # the spec and this check name the variables without values
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in assign.finditer(text):
            offenders.append((rel, match.group(1), match.group(2)))
    # os.environ lookups and the like are not literals
    assert [o for o in offenders if not o[2].startswith(("os.", "env", "source", "settings"))] == []
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for name in names:
        assert f"{name}=" in example, name
    for name in names:
        assert re.search(rf"^{name}=\s*$", "\n".join(example), re.MULTILINE), name


def test_secrets_not_in_repo() -> None:
    test_ops_no_secrets_in_repo()
