"""CONF checks of the portfolio layer."""

import re
from pathlib import Path

from hat_conf_repo import APP_ROOT

PORTFOLIO = APP_ROOT / "hat" / "engine" / "portfolio"
PURE = ("regime", "health", "allocator", "lifecycle", "universe")

# An attribute assignment, `setattr(..., "lifecycle_state", ...)`, or a bulk UPDATE through
# `.values(lifecycle_state=...)`.
WRITES = re.compile(
    r"\.lifecycle_state\s*(=(?!=)|\+=)|setattr\([^)]*lifecycle_state|\.values\([^)]*lifecycle_state"
)


def _python_files() -> list[Path]:
    return sorted(p for p in (APP_ROOT / "hat").rglob("*.py"))


def test_lifecycle_single_writer() -> None:
    """Only `sleeve_service.transition` assigns `hat_bot.lifecycle_state`, so no lifecycle change
    can skip its guard or its `sleeve.lifecycle_changed` event. Column definitions in the models
    and keyword arguments that build a new bot are not changes of an existing sleeve."""
    offenders = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if WRITES.search(line.split("#", 1)[0]):
                offenders.append((str(path.relative_to(APP_ROOT)), path.name, number))
    assert [(file, name) for file, name, _ in offenders] == [
        ("hat/engine/portfolio/sleeve_service.py", "sleeve_service.py")
    ]


def test_portfolio_rules_are_pure() -> None:
    """The regime, health, allocator, lifecycle and universe rules import no database, model,
    broker or file code, so the engine and the backtest apply the same rule."""
    forbidden = re.compile(
        r"^\s*(from|import)\s+(sqlalchemy|app\.database|app\.hat\.models"
        r"|app\.hat\.execution|app\.hat\.data|\.common|\.\w*service|\.jobs"
        r"|app\.hat\.engine\.portfolio\.(common|\w*service|jobs))\b",
        re.MULTILINE,
    )
    for name in PURE:
        text = (PORTFOLIO / f"{name}.py").read_text(encoding="utf-8")
        assert not forbidden.findall(text), name


def test_bot_sleeve_fields() -> None:
    """The sleeve record pins its strategy version (`live_version_id`) and its universe version
    (`universe_version_id`), carries `lifecycle_state` (one of the six of the lifecycle) and
    `risk_cluster`, and no risk number: the risk policy is the only home of sizes, weights and
    limits."""
    from app.hat.configs.records import Bot
    from app.hat.engine.portfolio import lifecycle

    assert {"live_version_id", "universe_version_id", "lifecycle_state", "risk_cluster"} <= set(
        Bot.FIELDS
    )
    assert set(Bot.ENUMS["lifecycle_state"]) == set(lifecycle.STATES)
    numbers = re.compile(r"(pct|weight|risk_amount|capital|limit|cap|size|qty|stop|equity)")
    assert [n for n in Bot.FIELDS if numbers.search(n)] == []
