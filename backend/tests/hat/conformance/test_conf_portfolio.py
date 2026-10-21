"""CONF checks of the portfolio layer (HAT-PORT-063, HAT-PORT-010, HAT-DEC-PL1)."""

import re
from pathlib import Path

from hat_conf_repo import APP_ROOT
from sqlalchemy import CheckConstraint

PORTFOLIO = APP_ROOT / "hat" / "engine" / "portfolio"
PURE = ("regime", "health", "allocator", "lifecycle", "universe")


def _python_files() -> list[Path]:
    return sorted(p for p in (APP_ROOT / "hat").rglob("*.py"))


def test_lifecycle_single_writer() -> None:
    """Only `service.transition` assigns `hat_bot.lifecycle_state`, so no lifecycle change can
    skip its guard or its `sleeve.lifecycle_changed` event (HAT-PORT-063). Column definitions in
    the models and keyword arguments that build a new bot are not changes of an existing sleeve."""
    offenders = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.lifecycle_state\s*(=(?!=)|\+=)", line.split("#", 1)[0]):
                offenders.append((str(path.relative_to(APP_ROOT)), path.name, number))
    assert [(file, name) for file, name, _ in offenders] == [
        ("hat/engine/portfolio/service.py", "service.py")
    ]


def test_portfolio_rules_are_pure() -> None:
    """The regime, health, allocator, lifecycle and universe rules import no database, model,
    broker or file code, so the engine and the backtest apply the same rule (HAT-PORT-010,
    HAT-DEC-PL1)."""
    forbidden = re.compile(
        r"^\s*(from|import)\s+(sqlalchemy|app\.brokers|app\.database|app\.hat\.models"
        r"|app\.hat\.execution|app\.hat\.data|\.service|\.jobs)\b",
        re.MULTILINE,
    )
    for name in PURE:
        text = (PORTFOLIO / f"{name}.py").read_text(encoding="utf-8")
        assert not forbidden.findall(text), name


def _bot_columns() -> dict[str, object]:
    from app.hat.models import HatBot

    return {c.name: c for c in HatBot.__table__.columns}


def test_bot_has_sleeve_columns() -> None:
    """`hat_bot` is the sleeve: it pins its strategy version (`live_version_id`) and its universe
    version (`universe_version_id`) by foreign key and carries `lifecycle_state` and
    `risk_cluster` (HAT-DEC-PL1)."""
    columns = _bot_columns()
    targets = {
        name: {fk.column.table.name for fk in columns[name].foreign_keys}  # type: ignore[attr-defined]
        for name in ("live_version_id", "universe_version_id")
    }
    assert targets == {
        "live_version_id": {"hat_strategy_version"},
        "universe_version_id": {"hat_universe_version"},
    }
    assert {"lifecycle_state", "risk_cluster"} <= set(columns)


def test_bot_sleeve_columns() -> None:
    """The sleeve row's lifecycle values are the six of the lifecycle, and it carries no risk
    number: the risk policy is the only home of sizes, weights and limits (HAT-PORT-010)."""
    from app.hat.engine.portfolio import lifecycle
    from app.hat.models import HatBot

    checks = [
        str(c.sqltext) for c in HatBot.__table__.constraints if isinstance(c, CheckConstraint)
    ]
    allowed = next(c for c in checks if "lifecycle_state" in c)
    assert set(re.findall(r"'(\w+)'", allowed)) == set(lifecycle.STATES)
    numbers = re.compile(r"(pct|weight|risk_amount|capital|limit|cap|size|qty|stop|equity)")
    assert [n for n in _bot_columns() if numbers.search(n)] == []
