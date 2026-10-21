"""CONF checks of the risk policy and the single risk authority (HAT-RISK-010/011/052/100/103,
HAT-DEC-B14, HAT-BOT-072). The expected fields and seed values are read from the 06 §1 table in
the spec markdown, not restated here."""

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
from hat_conf_spec import REPO_ROOT, SPEC_DIR
from sqlalchemy.orm import Session

from app.hat.risk.policy import (
    POLICY_FIELDS,
    PolicyError,
    parse_policy,
    seed_policy,
)

MEANING: dict[str, str] = {}
SPEC_06 = (SPEC_DIR / "06-risk-and-governance.md").read_text(encoding="utf-8")
BACKEND = REPO_ROOT / "backend"


def _section1_rows() -> dict[str, str]:
    """Field cell (backticks stripped, split on `, `) -> seed cell, from the §1 table."""
    block = SPEC_06[SPEC_06.index("## §1") : SPEC_06.index("#### HAT-RISK-010")]
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].startswith("`"):
            rows[cells[0]] = cells[2]
            MEANING[cells[0]] = cells[1]
    return rows


def _spec_paths() -> list[str]:
    """Every dotted field path named in the first column of the §1 table."""
    paths = []
    for cell in _section1_rows():
        paths.extend(re.findall(r"`([\w.\-]+)`", cell))
    return paths


def _number(cell: str) -> Decimal:
    return Decimal(re.match(r"\s*([0-9.]+)", cell).group(1))  # type: ignore[union-attr]


def test_risk__policy_schema_fields() -> None:
    """HAT-RISK-010: the policy document's fields are exactly the §1 table's, and a document with
    an extra or a missing field is refused."""
    spec_top = {path.split(".")[0] for path in _spec_paths()}
    assert spec_top == set(POLICY_FIELDS)
    doc = seed_policy().to_json()
    assert set(doc) == spec_top
    nested = {}
    for path in _spec_paths():
        head, _, tail = path.partition(".")
        if tail:
            nested.setdefault(head, set()).add(tail)
    assert set(doc["p_max"]) == nested["p_max"]
    assert set(doc["kill"]) == nested["kill"]
    assert set(doc["base_weight"]) == nested["base_weight"]
    extra = {**doc, "extra": 1}
    with pytest.raises(PolicyError):
        parse_policy(extra)
    for name in POLICY_FIELDS:
        without = {k: v for k, v in doc.items() if k != name}
        with pytest.raises(PolicyError):
            parse_policy(without)


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
    """HAT-RISK-011: the seeded policy equals the §1 seed column field by field."""
    rows = _section1_rows()
    doc = seed_policy().to_json()

    def row(field: str) -> str:
        return next(v for k, v in rows.items() if f"`{field}`" in k)

    assert Decimal(doc["risk_pct"]) == _number(row("risk_pct")) == Decimal("0.0025")
    for key in doc["p_max"]:
        assert Decimal(doc["p_max"][key]) == _number(row(f"p_max.{key}"))
    for name in ("adv_cap_pct", "global_open_risk_pct", "cluster_cap"):
        assert Decimal(doc[name]) == _number(row(name)), name
    for name in (
        "max_open_positions",
        "max_positions_per_industry",
        "max_entries_per_day",
        "max_stopouts_per_day",
    ):
        assert doc[name] == int(_number(row(name))), name
    assert Decimal(doc["kill"]["account_drawdown_pct"]) == _number(row("kill.account_drawdown_pct"))
    assert Decimal(doc["kill"]["bot_drawdown_pct"]) == _number(row("kill.bot_drawdown_pct"))
    weights = re.findall(r"[0-9.]+", row("base_weight.S1"))[:2]
    assert [Decimal(doc["base_weight"][k]) for k in ("S1", "S2")] == [Decimal(w) for w in weights]
    low, high = re.findall(r"[0-9.]+", row("multiplier_bounds"))[:2]
    assert (Decimal(doc["multiplier_bounds"]["lo"]), Decimal(doc["multiplier_bounds"]["hi"])) == (
        Decimal(low),
        Decimal(high),
    )
    row("exposure_ladder")
    ladder = next(v for k, v in MEANING.items() if "`exposure_ladder`" in k)
    levels = re.findall(r"L(\d) (\d+) pos / ([0-9.]+)x", ladder)
    assert [(f"L{n}", int(p), Decimal(m)) for n, p, m in levels] == [
        (
            k,
            doc["exposure_ladder"][k]["max_positions"],
            Decimal(doc["exposure_ladder"][k]["risk_mult"]),
        )
        for k in ("L1", "L2", "L3")
    ]
    up, down = re.findall(r"(?:up|down) after (\d+)", ladder)
    assert (
        doc["exposure_ladder"]["up_after_wins"],
        doc["exposure_ladder"]["down_after_losses"],
    ) == (
        int(up),
        int(down),
    )
    section5 = SPEC_06[SPEC_06.index("## §4") : SPEC_06.index("## §5")]
    assert "L1" in section5
    assert "L3" in section5
    health = doc["health_bands"]
    assert (Decimal(health["half_dd"]), Decimal(health["zero_dd"])) == (
        Decimal("0.08"),
        Decimal("0.12"),
    )  # "8% / 12% -> 1.0 / 0.5 / 0" (§1, health_bands)
    assert (Decimal(health["full_mult"]), Decimal(health["half_mult"])) == (1, Decimal("0.5"))
    assert parse_policy(doc) == seed_policy()


def test_initial_risk_policy_seed() -> None:
    """HAT-DEC-B14: seeding writes version 1, direction initial, effective at once, with the §1
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
    `evaluate_signal` lives in HAT's `risk/service.py`, not in the legacy risk manager."""
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
    manager = (APP_ROOT / "services" / "risk_manager.py").read_text(encoding="utf-8")
    assert "evaluate_signal" not in manager
    assert "size_position" not in manager


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


# SHA-256 of the existing risk test files as they were before the HAT risk code was added; the
# characterisation of the SmartTrader risk behaviour must not be edited to fit a change.
EXISTING_RISK_TESTS = {
    "tests/test_risk_fixes.py": "782f47a584847ea4aef1c9831b04d2a9a61c945e5d386e6a31103154feb3ebe9",
    "tests/test_live_trading_safety.py": (
        "95b18e2960c691d6b81d127209d0396bbdda86fef42cd25acdcc0414dd9e0915"
    ),
    "tests/test_market_hours_policy.py": (
        "518d543f103ad823479353ca1d82e9d9ac78ed380d69aa27a3dc5dc5a4404ef4"
    ),
}
RUN_UNMODIFIED = (*EXISTING_RISK_TESTS, "tests/test_order_execution_characterization.py")


def test_risk__existing_suite_green() -> None:
    """HAT-RISK-103: the existing risk and execution tests are byte-for-byte what they were and
    pass against the changed `RiskManager`, so no SmartTrader caller's risk behaviour moved."""
    for name, expected in EXISTING_RISK_TESTS.items():
        digest = hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} was edited: stop and explain, never adapt the test"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *RUN_UNMODIFIED,
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:]


def test_risk__manager_reads_no_hat_state() -> None:
    """HAT-RISK-103: the SmartTrader checks of `RiskManager` do not depend on HAT at all: the
    manager imports nothing from `app.hat` (the risk inversion of K1), and the existing exit checks
    name no HAT object."""
    path = APP_ROOT / "services" / "risk_manager.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hat_imports = (
        {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and re.match(
                r"(app\.|\.+)?hat(\.|$)", node.module
            )  # absolute or relative (`..hat.risk`)
        }
        | {
            f"{node.module or ''}.{alias.name}"  # `from app import hat`, `from .. import hat`
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "hat"
        }
        | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if re.match(r"(app\.)?hat(\.|$)", alias.name)
        }
    )
    assert hat_imports == set()
    existing = {"pre_trade_check", "pre_exit_check", "exit_order_check", "_check_market_hours"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in existing:
            source = ast.unparse(node)
            assert not re.search(r"\bhat\b|kill|HAT", source), node.name


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
