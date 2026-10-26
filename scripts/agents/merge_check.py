"""The orchestrator's merge check for one PR: prints the facts and one verdict line; never merges.

    python scripts/agents/merge_check.py <pr> [--sha <reported sha>]

Run from inside the repo with `gh` logged in. It checks, at the PR's head:

- the head is the builder's reported sha (`--sha`, a prefix is enough) and `mergeStateStatus` is
  `CLEAN`;
- every check passed or was skipped (the required ones and the rest, the TLA workflow too);
- the SonarCloud quality gate is `OK` (public API, project `<owner>_<repo>`);
- no unresolved review thread whose last comment is not by the repo owner;
- no added diff line that looks like a secret (a private key, a token, a password).

The code-review bot's status (`done` once it posted its review summary, else `pending`) is
reported but does not block: CI and the Sonar gate are the gate.

The last line is the verdict, `MERGE OK #<pr> <sha>` (exit 0) or
`NO MERGE #<pr> <sha>: <reasons>` (exit 1). The merge itself stays with the orchestrator:
`gh pr merge <pr> --squash --delete-branch --match-head-commit <sha>`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

REVIEW_BOT = "codeant"  # the review bot's login contains this
REVIEW_DONE = "Reviewed your PR"  # its summary comment once a review finished
SONAR_GATE = "https://sonarcloud.io/api/qualitygates/project_status"
OK_BUCKETS = frozenset({"pass", "skipping"})
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved path comments(last: 1) { nodes { author { login } } } }
      }
    }
  }
}
"""
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "quoted secret": re.compile(
        r"(?i)(?:secret|password|passwd|api_?key|consumer_key|access_token|totp|mpin)[a-z0-9_]*['\"]?"
        r"\s*[:=]\s*['\"][A-Za-z0-9+/_.~=-]{12,}['\"]"
    ),
    "env secret": re.compile(
        r"^\s*(?:export\s+)?[A-Z0-9_]*(?:SECRET|PASSWORD|KEY|TOKEN|TOTP|MPIN|PIN)[A-Z0-9_]*"
        r"=[^\s$<{]{8,}\s*$"
    ),
}


@dataclass
class Facts:
    pr: int
    head: str
    merge_state: str
    reported: str | None = None
    check_problems: list[str] = field(default_factory=list)
    sonar: str = "UNKNOWN"
    open_threads: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    review_bot: str = "pending"


def check_problems(checks: Iterable[Mapping[str, Any]]) -> list[str]:
    """`<name> <bucket>` of each check that neither passed nor was skipped; no checks at all is a
    problem too."""
    checks = list(checks)
    if not checks:
        return ["no checks"]
    return [
        f"{c['name']} {c.get('bucket') or c.get('state')}"
        for c in checks
        if c.get("bucket") not in OK_BUCKETS
    ]


def review_bot_status(comments: Iterable[Mapping[str, Any]], bot: str = REVIEW_BOT) -> str:
    """`done` once the bot posted its review summary, else `pending`."""
    for c in comments:
        login = ((c.get("author") or {}).get("login") or "").lower()
        if bot in login and REVIEW_DONE in (c.get("body") or ""):
            return "done"
    return "pending"


def open_threads(pages: Iterable[Mapping[str, Any]], owner: str) -> list[str]:
    """`path (last: login)` of each unresolved thread whose last comment is not the owner's,
    over every page of the GraphQL answer."""
    threads = [
        t
        for page in pages
        for t in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    ]
    out = []
    for t in threads:
        if t["isResolved"]:
            continue
        last = t["comments"]["nodes"]
        login = ((last[-1].get("author") or {}).get("login") or "ghost") if last else "ghost"
        if login != owner:
            out.append(f"{t.get('path') or '?'} (last: {login})")
    return out


def secret_hits(diff: str) -> list[str]:
    """`<file>:<kind>` for each added line of a unified diff that matches a secret pattern."""
    hits, path = [], "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++"):
            hits += [f"{path}:{k}" for k, rx in SECRET_PATTERNS.items() if rx.search(line[1:])]
    return sorted(set(hits))


def verdict(f: Facts) -> tuple[bool, str]:
    """(mergeable, the verdict line)."""
    reasons = []
    if f.reported and not f.head.startswith(f.reported):
        reasons.append(f"head {f.head[:7]} is not the reported {f.reported[:7]}")
    if f.merge_state != "CLEAN":
        reasons.append(f"merge state {f.merge_state}")
    if f.check_problems:
        reasons.append("checks: " + ", ".join(f.check_problems))
    if f.sonar != "OK":
        reasons.append(f"sonar gate {f.sonar}")
    if f.open_threads:
        reasons.append(f"{len(f.open_threads)} unanswered review threads")
    if f.secrets:
        reasons.append("possible secrets: " + ", ".join(f.secrets))
    if reasons:
        return False, f"NO MERGE #{f.pr} {f.head[:7]}: " + "; ".join(reasons)
    return True, f"MERGE OK #{f.pr} {f.head[:7]}"


def _gh(*args: str, empty_ok: bool = False) -> str:
    """gh's stdout. `gh pr checks` exits non-zero while a check fails or is pending (its JSON
    still counts) and when no check exists yet (`empty_ok`: an empty answer)."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)  # noqa: S607
    if not done.stdout and done.returncode and not empty_ok:
        sys.exit(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def _sonar(project: str, pr: int) -> str:
    query = urllib.parse.urlencode({"projectKey": project, "pullRequest": pr})
    try:
        with urllib.request.urlopen(f"{SONAR_GATE}?{query}", timeout=30) as r:  # noqa: S310 - https
            return json.load(r)["projectStatus"]["status"]
    except (OSError, ValueError, KeyError):
        return "UNREACHABLE"


def gather(pr: int, reported: str | None) -> Facts:
    repo = json.loads(_gh("repo", "view", "--json", "owner,name"))
    owner, name = repo["owner"]["login"], repo["name"]
    view = json.loads(_gh("pr", "view", str(pr), "--json", "headRefOid,mergeStateStatus,comments"))
    checks = json.loads(
        _gh("pr", "checks", str(pr), "--json", "name,state,bucket", empty_ok=True) or "[]"
    )
    threads = json.loads(
        _gh(
            "api",
            "graphql",
            "--paginate",
            "--slurp",
            "-f",
            f"query={THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr}",
        )
    )
    return Facts(
        pr=pr,
        head=view["headRefOid"],
        merge_state=view["mergeStateStatus"],
        reported=reported,
        check_problems=check_problems(checks),
        sonar=_sonar(f"{owner}_{name}", pr),
        open_threads=open_threads(threads, owner),
        secrets=secret_hits(_gh("pr", "diff", str(pr))),
        review_bot=review_bot_status(view["comments"]),
    )


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="merge_check.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("pr", type=int)
    p.add_argument("--sha", help="the head sha the builder reported")
    args = p.parse_args(argv)
    f = gather(args.pr, args.sha)
    print(
        f"head {f.head[:7]}  merge state {f.merge_state}  sonar {f.sonar}  "
        f"review bot {f.review_bot}"
    )
    for line in f.check_problems + f.open_threads + f.secrets:
        print(f"  {line}")
    ok, line = verdict(f)
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
