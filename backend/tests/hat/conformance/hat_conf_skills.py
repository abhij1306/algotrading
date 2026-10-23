"""Read the project-local verify skills (.claude/skills/verify-*/SKILL.md) for the conformance
checks, and compute the digest that records what a skill's coverage looked like when it was
last confirmed.

    python backend/tests/hat/conformance/hat_conf_skills.py verify-scheduler

prints the digest to write after "Covered digest:" once the verification has been re-confirmed.
"""

import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

REQUIRED_SECTIONS = (
    "Properties",
    "Run",
    "Bug or model error",
    "Updating",
    "Coverage",
    "Sync commit",
    "Scratch breaks",
)
PLACEHOLDER = re.compile(
    r"\b(TODO|TBD|FIXME|XXX|lorem ipsum)\b|<fill|\.\.\.\s*$", re.IGNORECASE | re.MULTILINE
)
PATH_TOKEN = re.compile(r"`((?:[\w.\-]+/)+[\w.\-]+\.[a-z]+)(?::[^`]*)?`")


def skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("verify-*/SKILL.md"))


def parse_skill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    meta: dict[str, str] = {}
    if front:
        for line in front.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    body = text[front.end() :] if front else text
    sections: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        heading = re.match(r"^## (.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return {"meta": meta, "sections": sections, "text": text}


def covered_files(coverage: str) -> list[str]:
    """Repository paths named in backticks in the Coverage section (`path` or `path:function`)."""
    return sorted(set(PATH_TOKEN.findall(coverage)))


def digest(paths: list[str]) -> str:
    """Sha256 over the sorted (path, sha256 of content) pairs of the covered files."""
    outer = hashlib.sha256()
    for rel in sorted(paths):
        inner = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        outer.update(f"{rel}\0{inner}\n".encode())
    return outer.hexdigest()


def property_ids(properties: str) -> list[str]:
    """The check ids a skill lists: the backticked lead of each `- ` bullet under Properties."""
    return re.findall(r"^- `([^`]+)`", properties, re.MULTILINE)


def break_ids(breaks: str) -> list[str]:
    return re.findall(r"^### (.+?)\s*$", breaks, re.MULTILINE)


DIGEST_LINE = re.compile(r"Covered digest: `([0-9a-f]{64})`")


def stale_skills() -> list[tuple[Path, str, str]]:
    """(SKILL.md, recorded digest, current digest) for every skill whose covered files changed."""
    stale = []
    for path in skill_files():
        skill = parse_skill(path)
        match = DIGEST_LINE.search(skill["sections"].get("Sync commit", ""))
        if not match:
            continue
        current = digest(covered_files(skill["sections"].get("Coverage", "")))
        if match.group(1) != current:
            stale.append((path, match.group(1), current))
    return stale


def main(args: list[str]) -> int:
    """`<skill>` prints its digest; `--stale` lists stale skills (exit 1 if any); `--refresh`
    rewrites their recorded digests and names them. Refresh only after rerunning each stale
    skill's checks and confirming its text still matches the code (builder-protocol.md)."""
    if args and args[0] in ("--stale", "--refresh"):
        stale = stale_skills()
        for path, old, new in stale:
            if args[0] == "--refresh":
                text = path.read_text(encoding="utf-8")
                path.write_text(
                    text.replace(f"Covered digest: `{old}`", f"Covered digest: `{new}`"),
                    encoding="utf-8",
                )
            sys.stdout.write(f"{path.parent.name}\n")
        return 1 if stale and args[0] == "--stale" else 0
    skill = parse_skill(SKILLS_DIR / args[0] / "SKILL.md")
    sys.stdout.write(digest(covered_files(skill["sections"]["Coverage"])) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
