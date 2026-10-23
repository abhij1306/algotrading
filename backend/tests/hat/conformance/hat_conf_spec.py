"""Read the requirement index of docs/invariants.md for the conformance checks. The index is the
data: each line names a `HAT-*` ID, a short title and the checks that prove it."""

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
INVARIANTS = REPO_ROOT / "docs" / "invariants.md"

# `- HAT-RISK-070: Invariant 1, stop never widens | TLA `StopPlacement!StopOnlyTightens`, ...`
# A planned item that is not built carries `PLANNED` before its checks and is not traced.
ENTRY = re.compile(r"^- (HAT-[A-Z]+-\d+): (.+?) \| (PLANNED )?(.+)$", re.MULTILINE)
METHOD = re.compile(r"\b(TLA|PBT|DB|CONF|OPS) `([^`]+)`")


@dataclass
class Requirement:
    id: str
    title: str
    verification: str
    planned: bool

    @property
    def methods(self) -> list[tuple[str, str]]:
        """(method, name) for every check the entry names."""
        return METHOD.findall(self.verification)


def requirements() -> list[Requirement]:
    """Every entry of the requirement index in docs/invariants.md."""
    text = INVARIANTS.read_text(encoding="utf-8")
    return [
        Requirement(m.group(1), m.group(2), m.group(4), bool(m.group(3)))
        for m in ENTRY.finditer(text)
    ]
