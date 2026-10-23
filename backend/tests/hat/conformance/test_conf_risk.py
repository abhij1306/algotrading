"""CONF checks of the risk policy schema and the single risk authority."""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hat_conf_model import hat_tables
from hat_conf_repo import APP_ROOT, REPO_ROOT
from sqlalchemy.orm import Session

from app.hat.risk.policy import (
    OPTIONAL_FIELDS,
    POLICY_FIELDS,
    PolicyError,
    parse_policy,
    seed_policy,
)

BACKEND = REPO_ROOT / "backend"


def _policy_paths() -> list[str]:
    """Every dotted field path of the policy document (the seed shows the nesting)."""
    paths = []
    for key, value in seed_policy().to_json().items():
        paths.append(key)
        if isinstance(value, dict):
            paths.extend(f"{key}.{inner}" for inner in value)
    return paths


def test_risk__policy_schema_fields() -> None:
    """The policy document's fields are exactly the seed's, and a document with
    an extra or a missing field is refused, except an optional one (`weights_confirmed`), which
    reads as its fail-closed default."""
    doc = seed_policy().to_json()
    assert set(doc) == set(POLICY_FIELDS)
    extra = {**doc, "extra": 1}
    with pytest.raises(PolicyError):
        parse_policy(extra)
    for name in POLICY_FIELDS:
        without = {k: v for k, v in doc.items() if k != name}
        if name in OPTIONAL_FIELDS:
            assert parse_policy(without).to_json()[name] == OPTIONAL_FIELDS[name]
            continue
        with pytest.raises(PolicyError):
            parse_policy(without)


def test_risk__no_risk_keys_in_strategy_schema() -> None:
    """No bot, strategy or strategy-version column carries a policy field."""
    names = set(POLICY_FIELDS) | {p.split(".")[-1] for p in _policy_paths()}
    tables = hat_tables()
    for table in ("hat_bot", "hat_strategy", "hat_strategy_version"):
        for column in tables[table].columns:
            assert column.name not in names, f"{table}.{column.name}"
            assert not re.search(r"risk_pct|p_max|adv_cap|drawdown|stopout", column.name), (
                table,
                column.name,
            )


def test_initial_risk_policy_seed() -> None:
    """Seeding writes version 1, direction initial, effective at once, with the seed
    policy and both governance events, and seeding again changes nothing."""
    from hat_support import memory_engine
    from sqlalchemy import select

    from app.hat.models import HatEvent, HatRiskPolicyVersion
    from app.hat.risk.policy_service import seed_initial_policy
    from app.hat.settings import load_settings

    user = load_settings().user_id
    with Session(memory_engine()) as session:
        first = seed_initial_policy(session, user, datetime.now(UTC))
        again = seed_initial_policy(session, user, datetime.now(UTC))
        session.commit()
        assert first.id == again.id
        rows = session.scalars(select(HatRiskPolicyVersion)).all()
        assert len(rows) == 1
        assert (rows[0].version, rows[0].direction) == (1, "initial")
        assert rows[0].policy == seed_policy().to_json()
        kinds = sorted(e.event_type for e in session.scalars(select(HatEvent)))
        assert kinds == ["risk.change_effective", "risk.change_requested"]


# --- the single authority ----------------------------------------------------------------------


def _calls_and_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            found.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
    return found


def test_risk__single_sizing_path() -> None:
    """`size_position` is called in one place, the decision behind `evaluate_signal`;
    no other module of the application sizes a position or imports the sizing module, and
    `evaluate_signal` lives in HAT's `risk/service.py` and no legacy risk manager exists."""
    users = []
    for path in APP_ROOT.rglob("*.py"):
        if path.name == "sizing.py" and path.parent.name == "risk":
            continue
        names = _calls_and_imports(path)
        if names & {"size_position", "SizingInputs"}:
            users.append(path.relative_to(APP_ROOT).as_posix())
    assert users == ["hat/risk/decision.py"]
    service = (APP_ROOT / "hat" / "risk" / "service.py").read_text(encoding="utf-8")
    assert re.search(r"^def evaluate_signal\(", service, re.MULTILINE)
    assert "decide(" in service
    assert not (APP_ROOT / "services" / "risk_manager.py").exists()


OVERRIDE_WORDS = re.compile(
    r"override|bypass|force|ignore_(gate|risk|kill)|skip_(gate|risk|kill)", re.I
)


def test_risk__no_gate_override_path() -> None:
    """No function of the risk modules (`evaluate_signal` included) takes or names
    an override, bypass or force switch, so the regime gate and the kills have no way around
    them."""
    paths = list((APP_ROOT / "hat" / "risk").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                assert not any(OVERRIDE_WORDS.search(a) for a in args), (path.name, node.name)
                assert not OVERRIDE_WORDS.search(node.name), (path.name, node.name)
            elif isinstance(node, ast.ClassDef) and path.parent.name == "risk":
                assert not OVERRIDE_WORDS.search(node.name), (path.name, node.name)


MANUAL_TRADING_GONE = (
    "services/risk_manager.py",
    "services/order_execution_service.py",
    "services/position_sync_service.py",
    "services/live_exit_service.py",
    "services/execution",
    "brokers",
    "routers/trading.py",
    "routers/terminal.py",
    "routers/unified.py",
    "routers/portfolio.py",
    "routers/activity.py",
    "models/order.py",
    "models/live_position.py",
    "models/live_trade.py",
)


def test_risk__no_manual_trading_path() -> None:
    """: the legacy manual-trading modules (risk
    manager, order execution, position sync, live exit, the order routes and the brokers package)
    are gone, so the only way to an order is HAT's `evaluate_signal` and adapter; no class named
    as a risk manager or an order-execution service exists."""
    assert [rel for rel in MANUAL_TRADING_GONE if (APP_ROOT / rel).exists()] == []
    lookalike = re.compile(r"class \w*(RiskManager|OrderExecutionService|PositionSyncService)\b")
    offenders = [
        path.relative_to(APP_ROOT).as_posix()
        for path in sorted(APP_ROOT.rglob("*.py"))
        if lookalike.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_risk__no_legacy_halt_read() -> None:
    """No module of `app/hat` names the VCP halt flag (`TRADING_HALTED`,
    `smarttrader_halt*`) or reads the legacy `system_config` table, so HAT's own kill switches are
    the only halt of an entry."""
    banned = re.compile(
        r"TRADING_HALTED|smarttrader_halt|SystemConfig|system_config", re.IGNORECASE
    )
    offenders = [
        (path.relative_to(APP_ROOT).as_posix(), match.group(0))
        for path in sorted((APP_ROOT / "hat").rglob("*.py"))
        for match in banned.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
