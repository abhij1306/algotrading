"""CONF checks of the risk policy and the single risk authority (HAT-RISK-010/011/052/100/103,
HAT-DEC-B14, HAT-BOT-072). The seed values are the owner's numbers, pinned here and in
docs/decisions.md; a change to either is a deliberate, reviewed one."""

import ast
import hashlib
import re
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hat_conf_model import hat_tables
from hat_conf_repo import APP_ROOT
from hat_conf_spec import REPO_ROOT
from sqlalchemy.orm import Session

from app.hat.risk.policy import (
    OPTIONAL_FIELDS,
    POLICY_FIELDS,
    PolicyError,
    parse_policy,
    seed_policy,
)

OWNER_SEED = {
    "risk_pct": "0.0025",
    "p_max": {"stage2-contraction-breakout": "0.20", "momentum-breakout-ep": "0.25"},
    "adv_cap_pct": "0.01",
    "global_open_risk_pct": "0.015",
    "max_open_positions": 6,
    "max_positions_per_industry": 2,
    "max_entries_per_day": 3,
    "max_stopouts_per_day": 3,
    "cooldown_sessions": 0,
    "exposure_ladder": {
        "L1": {"max_positions": 2, "risk_mult": "0.5"},
        "L2": {"max_positions": 4, "risk_mult": "1.0"},
        "L3": {"max_positions": 6, "risk_mult": "1.0"},
        "up_after_wins": 2,
        "down_after_losses": 2,
    },
    "kill": {"account_drawdown_pct": "0.10", "bot_drawdown_pct": "0.10"},
    "base_weight": {"S1": "0.50", "S2": "0.50"},
    "cluster_cap": "1.00",
    "multiplier_bounds": {"lo": "0.25", "hi": "1.0"},
    "health_bands": {"half_dd": "0.08", "zero_dd": "0.10", "full_mult": "1.0", "half_mult": "0.5"},
    "weights_confirmed": True,
}
BACKEND = REPO_ROOT / "backend"


def _spec_paths() -> list[str]:
    """Every dotted field path of the policy document (the seed shows the nesting)."""
    paths = []
    for key, value in OWNER_SEED.items():
        paths.append(key)
        if isinstance(value, dict):
            paths.extend(f"{key}.{inner}" for inner in value)
    return paths


def test_risk__policy_schema_fields() -> None:
    """HAT-RISK-010: the policy document's fields are exactly the seed's, and a document with
    an extra or a missing field is refused, except an optional one (`weights_confirmed`), which
    reads as its fail-closed default."""
    spec_top = set(OWNER_SEED)
    assert spec_top == set(POLICY_FIELDS)
    doc = seed_policy().to_json()
    assert set(doc) == spec_top
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
    assert OPTIONAL_FIELDS == {"weights_confirmed": False, "cooldown_sessions": 0}


def test_risk__no_risk_keys_in_strategy_schema() -> None:
    """HAT-RISK-010: no bot, strategy or strategy-version column carries a policy field."""
    names = set(POLICY_FIELDS) | {p.split(".")[-1] for p in _spec_paths()}
    tables = hat_tables()
    for table in ("hat_bot", "hat_strategy", "hat_strategy_version"):
        for column in tables[table].columns:
            assert column.name not in names, f"{table}.{column.name}"
            assert not re.search(r"risk_pct|p_max|adv_cap|drawdown|stopout", column.name), (
                table,
                column.name,
            )


def test_risk__initial_seed_equals_spec() -> None:
    """HAT-RISK-011: the seeded policy is the owner's seed, field by field."""
    doc = seed_policy().to_json()
    assert doc == OWNER_SEED
    assert parse_policy(doc) == seed_policy()


def test_initial_risk_policy_seed() -> None:
    """HAT-DEC-B14: seeding writes version 1, direction initial, effective at once, with the seed
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
        assert Decimal(rows[0].policy["risk_pct"]) == Decimal("0.0025")
        kinds = sorted(e.event_type for e in session.scalars(select(HatEvent)))
        assert kinds == ["risk.change_effective", "risk.change_requested"]


def test_risk_policy_seed_bot_limits() -> None:
    """HAT-BOT-072: the seed holds p_max 0.20 for S1, 0.25 for S2 and 6 open positions."""
    doc = seed_policy().to_json()
    assert Decimal(doc["p_max"]["stage2-contraction-breakout"]) == Decimal("0.20")
    assert Decimal(doc["p_max"]["momentum-breakout-ep"]) == Decimal("0.25")
    assert doc["max_open_positions"] == 6
    assert doc["exposure_ladder"]["L3"]["max_positions"] == 6


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
    """HAT-RISK-100: `size_position` is called in one place, the decision behind `evaluate_signal`;
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
    """HAT-RISK-052: no function of the risk modules (`evaluate_signal` included) takes or names
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


# HAT's own safety properties: the Hypothesis properties whose failure would let an order past a
# kill, a stop, a cap, the session or the broker's fail-closed rules. file (relative to
# `backend/tests/hat`) -> property names. K4 replaced the digest pin of the legacy risk tests
# (deleted with the manual-trading code) with this pin.
SAFETY_PROPERTIES = {
    "properties/test_caps.py": (
        "test_exits_never_blocked",
        "test_regime_gate_blocks_entries",
        "test_entry_requires_stop",
        "test_stale_blocks_entry",
        "test_market_hours_only",
        "test_hat_kill_is_authority",
        "test_decision_total_on_garbage",
        "test_global_open_risk",
        "test_max_chase_always",
        "test_kill_thresholds",
    ),
    "properties/test_tags.py": (
        "test_a_37th_order_is_refused",
        "test_decode_refuses_what_is_not_a_hat_tag",
        "test_invalid_intents_raise_before_any_sdk_call",
        "test_broker_errors_fail_closed",
        "test_refusals_happen_before_the_broker",
        "test_a_timeout_is_looked_up_before_any_retry",
    ),
    "properties/test_fyers_broker.py": (
        "test_nothing_is_sent_while_the_circuit_is_open",
        "test_only_an_explicit_error_is_a_rejection",
    ),
    "properties/test_kill.py": ("test_kill_service_matches_model",),
    "properties/test_sizing.py": ("test_risk_bounded",),
}
# SHA-256 of `ast.dump` of each pinned property (decorators, strategies and body). Weakening one
# to fit a change fails here: stop and explain, then re-pin it with the reason in the PR.
SAFETY_DIGESTS = {
    # Re-pinned in PR 46 (D128): the oracle's cap 3% -> 1.5%, the same property otherwise.
    "test_global_open_risk": ("9beb2c6119aa01dbe1e84c2245285a17aebd6163ab3ed52fc25c2ec0bec3d4c5"),
    "test_exits_never_blocked": (
        "8274671f6c65ec6b48cb77db76a0aa5282381752bbfaf64b6be93aa0f4609e7b"
    ),
    "test_regime_gate_blocks_entries": (
        "0c707221de86b481e12f5a96d3317ec48b42c26131c08d61f7b355597be01988"
    ),
    # Re-pinned in PR 46 (D128): the oracle's account kill 15% -> 10%, the same property otherwise.
    "test_kill_thresholds": ("d3bd9c3b3f4976c62b97ff3fa7d173f62e2bb82cdc9e30dde057e5a1ab3a8327"),
    "test_entry_requires_stop": (
        "82c6b69044d9b2c49bb8924cce9b62a3bf12162706983342467a10d11e3068c6"
    ),
    "test_stale_blocks_entry": ("941c3b48c6e9faf4eb5cfe1e7db5b2f65602e8d7ed5cf5370fb347e738ebbb2a"),
    "test_market_hours_only": ("ac1870bff30c18760974d6b40183b6629e2f7e73178c21bde2a69b64311382f5"),
    "test_max_chase_always": ("9bc34825f7d88f17b57dacc5e8bedba2733967f06509e39547ec303651e10c1f"),
    "test_hat_kill_is_authority": (
        "6879437c7689ac2c5df662afee6e0936010ad411afd9a327f93da4aa84817a2f"
    ),
    "test_decision_total_on_garbage": (
        "b60f5ce9e55ee26e6ff95ed9f5363805289d9e3328441567b3e9dc327b9c02c1"
    ),
    "test_decode_refuses_what_is_not_a_hat_tag": (
        "9ea35cd14baca091876c2ce8755b5397caf21f9a7e26b98f946b57225d3287f3"
    ),
    "test_a_37th_order_is_refused": (
        "b29b72acbe5d4bff7a8b950323017e3863ef712866d8f872583a024cb955f9ec"
    ),
    "test_invalid_intents_raise_before_any_sdk_call": (
        "b794ceb67d07b8b3881e61633cdd1efa235f20ca9c8677ddbce614183f832532"
    ),
    "test_broker_errors_fail_closed": (
        "d9c116d96a4842f25e19a8a9b6ab348acb32b6f08dff6eda2722bcfc4070dc37"
    ),
    # Re-pinned in PR 28 (stronger, not weaker; HAT-BRK-102, -103): an entry is now placed exactly
    # once, also with a lagging order book. The added day-stop cases keep the lookup-before-retry
    # check; a stop on a lagging book may rest twice (the known window in verify-execution).
    "test_a_timeout_is_looked_up_before_any_retry": (
        "5fda6dbac35be3e8a2c8c358bdd43968e8c2bb445d4906bff142237d258a7353"
    ),
    # Re-pinned in PR 24 (owner decision D118, trading-safety rules 5 and 7 reworded): the
    # reconciliation gate holds entries only, so a stop or a sale must now pass while the trade
    # date is not reconciled; every other refusal is unchanged. Re-pinned in PR 29 (stronger,
    # HAT-EXEC-012, HAT-BRK-092): an entry also needs GTT for delivery in both the stored
    # capabilities and the adapter's own; a stop or a sale still passes either way.
    "test_refusals_happen_before_the_broker": (
        "92af53d09331ab7bef7b1fa4abcd51c61b918d45c1cd9507b98a156fc2c5d95e"
    ),
    "test_nothing_is_sent_while_the_circuit_is_open": (
        "56c22d14f1156f34bf3893a3d34265da99af6a590d6c4eab9dab33635e00b300"
    ),
    "test_only_an_explicit_error_is_a_rejection": (
        "930aab8d75d7762a6a2accc373f9526881a90bb4b67b8c68d51cbdddb31e25d2"
    ),
    # Re-pinned in PR 17 (stronger, not weaker): the account's health ops gained a source (the
    # gate's data_health, the feed's feed_health) and the model checks each source apart.
    "test_kill_service_matches_model": (
        "367eacedc535ee25ce0975b4d47c75c1c49fe5fde7af762f7775c58ad99b250d"
    ),
    "test_risk_bounded": ("ee5c00eacc3a558352a78b9ae96ded119423710cae4aed194e7ac76b62dca76b"),
}
HAT_TESTS = BACKEND / "tests" / "hat"


def _pinned_properties() -> dict[tuple[str, str], ast.FunctionDef]:
    found = {}
    for rel, names in SAFETY_PROPERTIES.items():
        tree = ast.parse((HAT_TESTS / rel).read_text(encoding="utf-8"))
        defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in names:
            if name in defs:
                found[(rel, name)] = defs[name]
    return found


def _is_hypothesis_property(node: ast.FunctionDef) -> bool:
    return any(
        (d.func if isinstance(d, ast.Call) else d).id == "given"
        for d in node.decorator_list
        if isinstance(d.func if isinstance(d, ast.Call) else d, ast.Name)
    )


def _property_digest(node: ast.FunctionDef) -> str:
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()


def test_risk__hat_safety_properties_pinned() -> None:
    """HAT-RISK-103: HAT's safety properties (the exits, regime gate, stop, staleness, session,
    kill, cap and fail-closed broker rules) all exist, are still Hypothesis properties (not
    examples) and are byte-for-byte what they were when pinned, and they pass against the real
    `evaluate_signal` and adapter. Editing one to fit a change is refused."""
    pinned = _pinned_properties()
    expected = {(rel, name) for rel, names in SAFETY_PROPERTIES.items() for name in names}
    assert set(pinned) == expected, sorted(expected - set(pinned))
    for (rel, name), node in pinned.items():
        assert _is_hypothesis_property(node), f"{rel}::{name} is no longer a @given property"
        assert _property_digest(node) == SAFETY_DIGESTS[name], (
            f"{rel}::{name} was edited: stop and explain, never weaken the property to fit a change"
        )
    assert set(SAFETY_DIGESTS) == {name for _, name in expected}
    ids = [f"tests/hat/{rel}::{name}" for rel, name in sorted(expected)]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", *ids],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:]


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
    """HAT-RISK-103 (K4, owner: no manual trading): the legacy manual-trading modules (risk
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
    """HAT-RISK-104: no module of `app/hat` names the VCP halt flag (`TRADING_HALTED`,
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
