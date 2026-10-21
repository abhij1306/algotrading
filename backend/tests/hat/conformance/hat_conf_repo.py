"""Repository helpers for the conformance checks: file lists and the Python import graph."""

import ast
import shutil
import subprocess
from pathlib import Path

from hat_conf_spec import REPO_ROOT

APP_ROOT = REPO_ROOT / "backend" / "app"


def repo_files() -> list[Path]:
    """Tracked files plus untracked, not-ignored ones (a new file counts before commit)."""
    git = shutil.which("git") or "git"
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [REPO_ROOT / name for name in out.split("\0") if name and (REPO_ROOT / name).is_file()]


def module_name(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path) -> set[str]:
    """Absolute module names a file imports (relative imports resolved), including every
    `from x import y` candidate `x.y`, so a submodule import is seen."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module_name(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def build_graph(root: Path = APP_ROOT) -> dict[str, set[str]]:
    return {
        module_name(path): imports_of(path)
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def reaches(graph: dict[str, set[str]], start: str, forbidden: tuple[str, ...]) -> list[str]:
    """The shortest import chain from `start` to a module equal to or under a forbidden prefix,
    or [] when none is reachable. Chains follow only modules present in the graph."""

    def hit(name: str) -> bool:
        return any(name == f or name.startswith(f + ".") for f in forbidden)

    seen = {start}
    queue = [[start]]
    while queue:
        chain = queue.pop(0)
        for target in sorted(graph.get(chain[-1], ())):
            if hit(target):
                return [*chain, target]
            if target in graph and target not in seen:
                seen.add(target)
                queue.append([*chain, target])
    return []
