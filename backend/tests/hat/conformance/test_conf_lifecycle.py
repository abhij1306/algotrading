"""CONF checks of the trade lifecycle: HAT-EXEC-040, the states and transitions the engine holds
equal the table of 07 section 4, row for row; HAT-EXEC-042 (PR 36), only the trade journal
writes a trade's state."""

import ast
import re

from hat_conf_spec import REPO_ROOT, SPEC_DIR

from app.hat.engine import lifecycle
from app.hat.models.runtime import TRADE_STATES


def _section() -> str:
    text = (SPEC_DIR / "07-execution-and-brokers.md").read_text(encoding="utf-8")
    start = text.index("## §4 Trade lifecycle")
    return text[start : text.index("#### HAT-EXEC-040", start)]


def _rows(section: str, header: str) -> list[list[str]]:
    lines = section[section.index(header) :].splitlines()[2:]
    rows = []
    for line in lines:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
    return rows


def test_lifecycle_transitions_match_spec() -> None:
    section = _section()
    states = [r[1] for r in _rows(section, "| # | State | Kind |")]
    kinds = {r[1]: r[2].split(" ")[0] for r in _rows(section, "| # | State | Kind |")}
    assert len(states) == 11
    assert set(states) == set(lifecycle.STATES) == set(TRADE_STATES)
    assert {s for s, k in kinds.items() if k == "ending"} == set(lifecycle.ENDINGS)
    spec_pairs = set()
    for src, dst, *_ in _rows(section, "| From | To | Driving event | Condition |"):
        if src == "(none)":
            continue
        spec_pairs |= {(re.sub(r"\s+", "", s), dst) for s in src.split(",")}
    code_pairs = {(src, dst) for (src, _), dst in lifecycle.TRANSITIONS.items()}
    assert code_pairs == spec_pairs


APP = REPO_ROOT / "backend" / "app" / "hat"
JOURNAL = APP / "engine" / "journal.py"
TRADE_TABLES = {"HatSignal", "HatPosition"}


def _state_writes(tree: ast.AST) -> list[int]:
    """The lines that write a `state` attribute or column: `x.state = ...` (any assignment form,
    `self.state` of a class's own field excepted unless the class is a trade table),
    `setattr(x, "state", ...)`, and an `update(HatSignal|HatPosition)` (or `models.HatSignal`)
    whose `values` sets `state`."""
    lines = [
        sub.lineno
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name in TRADE_TABLES
        for sub in ast.walk(cls)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.ctx, ast.Store)
        and sub.attr == "state"
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "self"
    ]
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr == "state"
                    and not (isinstance(sub.value, ast.Name) and sub.value.id == "self")
                ):
                    lines.append(sub.lineno)
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "state"
        ):
            lines.append(node.lineno)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
            keys = {k.arg for k in node.keywords} | {
                k.value
                for arg in node.args
                if isinstance(arg, ast.Dict)
                for k in arg.keys
                if isinstance(k, ast.Constant)
            }
            tables = {n.id for n in ast.walk(node.func.value) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(node.func.value) if isinstance(n, ast.Attribute)
            }
            if "state" in keys and tables & TRADE_TABLES:
                lines.append(node.lineno)
    return lines


def test_journal_only_writers() -> None:
    """HAT-EXEC-042 (PR 36, architecture audit F1): no module of `app/hat` but the trade journal
    (`engine/journal.py`) writes `.state`, so every state change passes the journal's check of
    the step against the table before its event is written. A row is created at its first
    state; `self.state` is a class's own field (`LifecycleStateError`)."""
    found = []
    for path in sorted(APP.rglob("*.py")):
        if path == JOURNAL:
            continue
        for line in _state_writes(ast.parse(path.read_text(encoding="utf-8"))):
            found.append(f"{path.relative_to(APP)}:{line}")
    assert found == []
    assert _state_writes(ast.parse(JOURNAL.read_text(encoding="utf-8")))  # the check sees writes
