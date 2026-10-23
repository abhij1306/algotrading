"""CONF check that the TLA+ models stay tied to the code: every action of a model's Next names a
function that exists. It runs on every PR, so a rename is caught even when tla.yml is skipped."""

import ast
import re
from pathlib import Path

from hat_conf_repo import REPO_ROOT

TLA_DIR = REPO_ROOT / "verification" / "tla"


def _models() -> list[Path]:
    return sorted(TLA_DIR.glob("*.tla"))


def _operators(text: str) -> dict[str, str]:
    """Name -> the comment lines immediately above its definition."""
    lines = text.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(\w+)(?:\([^)]*\))?\s*==", line)
        if match:
            above = []
            cursor = index - 1
            while cursor >= 0 and lines[cursor].startswith("\\*"):
                above.insert(0, lines[cursor])
                cursor -= 1
            found[match.group(1)] = "\n".join(above)
    return found


def _next_actions(text: str) -> list[str]:
    body = re.search(r"^Next ==\n(.*?)\n\n", text, re.DOTALL | re.MULTILINE)
    assert body, "no Next relation"
    lines = text.splitlines()
    actions = set()
    for index, line in enumerate(lines):
        match = re.match(r"^(\w+)(?:\([^)]*\))?\s*==", line)
        if not match:
            continue
        block = []
        for follow in lines[index:]:
            if not follow.strip() and block:
                break
            block.append(follow)
        if "'" in "\n".join(block):  # an action assigns primed variables
            actions.add(match.group(1))
    names = re.findall(r"\b(\w+)\b", body.group(1))
    return sorted({n for n in names if n in actions and n != "Next"})


def _function_exists(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == name
        for node in ast.walk(tree)
    )


def test_tla_actions_mapped() -> None:
    """Every action of Next names `backend/app/hat/...py:function` that exists."""
    for model in _models():
        text = model.read_text(encoding="utf-8")
        actions = _next_actions(text)
        assert len(actions) >= 3, model.name
        operators = _operators(text)
        for action in actions:
            match = re.search(r"\\\* code: (backend/app/hat/[\w/]+\.py):(\w+)", operators[action])
            assert match, f"{model.name}: action {action} has no code comment"
            target = REPO_ROOT / match.group(1)
            assert target.exists(), (action, match.group(1))
            assert _function_exists(target, match.group(2)), (action, match.group(2))
