"""CONF money.one_decimal_boundary (HAT-SCH-014; PR 75c, architecture A-F7): `app/hat/money.py`
is the one place HAT turns an untrusted number into a Decimal (`exact`, `require_exact`)."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
BOUNDARY = "app/hat/money.py"
# Lane E's files, left to its own PR (PR 75c follow-ups); each moves to `money.exact` with it and
# leaves this set in the same change.
LANE_E = {
    "app/hat/engine/backtest.py",
    "app/hat/backtest/runs.py",
    "app/hat/backtest/market.py",
    "app/hat/engine/portfolio/jobs.py",
    "app/hat/engine/portfolio/service.py",
}
_TEXT = {"str", "repr", "float"}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _coercions(tree: ast.Module) -> list[int]:
    """Lines that read a number into a Decimal by hand: `Decimal(str(x))`, `Decimal(repr(x))`,
    `Decimal(float(x))`, `Decimal(x.strip())`, `Decimal(row["k"])` (a payload or reply value)
    and the like, or any `Decimal(...)` inside a `try` (a guard against what it cannot read)."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.handlers:
            guarded.update(id(inner) for stmt in node.body for inner in ast.walk(stmt))
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _name(node.func) == "Decimal" and node.args):
            continue
        arg = node.args[0]
        by_text = isinstance(arg, ast.Subscript) or (
            isinstance(arg, ast.Call)
            and (_name(arg.func) in _TEXT or isinstance(arg.func, ast.Attribute))
        )
        if by_text or id(node) in guarded:
            lines.append(node.lineno)
    return lines


def hand_coercions() -> dict[str, list[int]]:
    found = {}
    for path in sorted((BACKEND / "app" / "hat").rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel == BOUNDARY or rel in LANE_E:
            continue
        lines = _coercions(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        if lines:
            found[rel] = lines
    return found


def test_money__one_decimal_boundary() -> None:
    """No HAT module outside `money.py` reads a number into a Decimal by hand, so every reader
    keeps one rule for floats, text, signs and rounding. Harm: the next author copies the nearest
    private `_dec`, and two readers of one tick or reply disagree (a NaN passes one, a numpy
    float breaks the other). Lane E's files are listed and must leave the list when they move."""
    assert hand_coercions() == {}
    for rel in sorted(LANE_E):  # an entry with nothing left to move is stale
        assert _coercions(ast.parse((BACKEND / rel).read_text(encoding="utf-8"))), rel
