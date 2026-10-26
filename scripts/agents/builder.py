"""Start a builder agent on a fresh worktree, or clean one up after its PR merged.

    python scripts/agents/builder.py start <slug> --brief FILE [--dry-run]
    python scripts/agents/builder.py cleanup <slug> [--force] [--dry-run]

Run from inside the repo, inside herdr (the orchestrator's session). `start`:

1. `git fetch origin`, then a worktree `<worktrees>/<slug>` on branch `hat/<slug>`: from
   `origin/main`, or from `origin/hat/<slug>` when the branch exists (a PR whose builder stopped);
   an existing worktree is reused;
2. a herdr tab in it (`herdr tab create --cwd <dir> --label <slug> --no-focus`);
3. the agent in the tab's pane (`herdr agent start <slug> --kind claude --pane <pane> --
   --permission-mode auto`), answering a first-start folder-trust prompt;
4. the brief as its first prompt, then waits until the agent is working.

It prints the pane id, which the watch command takes (docs/agents/orchestrator.md).

`cleanup` refuses while the branch's PR is open (`--force` overrides, and also removes a dirty
worktree), then closes the agent's tab (which ends the agent), removes the worktree and deletes
the branch, local and on origin. `<worktrees>` is `--worktrees`, else `HAT_WORKTREES`, else
`wt/hat` beside the main checkout. `--dry-run` prints the commands instead of running them.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BRANCH_PREFIX = "hat/"
AGENT_ARGS = ("--kind", "claude")
CLAUDE_ARGS = ("--permission-mode", "auto")


def branch_of(slug: str) -> str:
    if not slug or slug.startswith("-") or not all(c.isalnum() or c in "-_." for c in slug):
        raise ValueError(f"bad slug {slug!r}: letters, digits, '-', '_' and '.' only")
    return BRANCH_PREFIX + slug


def default_worktrees(common_git_dir: Path, env: Mapping[str, str]) -> Path:
    """`HAT_WORKTREES`, else `wt/hat` beside the main checkout (the parent of its .git)."""
    if env.get("HAT_WORKTREES"):
        return Path(env["HAT_WORKTREES"])
    return common_git_dir.resolve().parent.parent / "wt" / "hat"


def pane_of(tab_create: Mapping[str, Any]) -> str:
    """The new tab's pane id from `herdr tab create`'s JSON."""
    return tab_create["result"]["root_pane"]["pane_id"]


def find_agent(agent_list: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    return next((a for a in agent_list["result"]["agents"] if a.get("name") == name), None)


def find_tab(tab_list: Mapping[str, Any], label: str) -> str | None:
    return next((t["tab_id"] for t in tab_list["result"]["tabs"] if t.get("label") == label), None)


def open_prs(pr_list: Sequence[Mapping[str, Any]]) -> list[int]:
    return [p["number"] for p in pr_list if p.get("state") == "OPEN"]


class Runner:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def run(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a command (print it on a dry run); a check failure exits with its stderr."""
        if self.dry_run:
            print("+", shlex.join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # fixed git/gh/herdr commands (a taint-analysis false positive): the one operator input,
        # the slug, is restricted by branch_of to [A-Za-z0-9._-], never leading with "-"
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)  # NOSONAR
        if check and done.returncode:
            sys.exit(f"{shlex.join(cmd[:4])} failed: {(done.stderr or done.stdout).strip()[:400]}")
        return done

    def read(self, *cmd: str) -> str:
        """Run a read-only command even on a dry run."""
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # NOSONAR


def _remote_has(r: Runner, branch: str) -> bool:
    return bool(r.read("git", "ls-remote", "--heads", "origin", branch).strip())


def start(r: Runner, slug: str, brief: str, worktrees: Path) -> None:
    branch, wt = branch_of(slug), worktrees / slug
    r.run("git", "fetch", "origin")
    if wt.exists():
        print(f"reusing {wt}")
    elif _remote_has(r, branch):
        r.run("git", "worktree", "add", "-B", branch, str(wt), f"origin/{branch}")
    else:
        r.run("git", "worktree", "add", "-b", branch, str(wt), "origin/main")
    tab = r.run("herdr", "tab", "create", "--cwd", str(wt), "--label", slug, "--no-focus")
    pane = "<pane>" if r.dry_run else pane_of(json.loads(tab.stdout))
    started = r.run(
        "herdr",
        "agent",
        "start",
        slug,
        *AGENT_ARGS,
        "--pane",
        pane,
        "--",
        *CLAUDE_ARGS,
        check=False,
    )
    if started.returncode:
        if "agent_not_ready" not in started.stdout + started.stderr:
            sys.exit(f"agent start failed: {(started.stderr or started.stdout).strip()[:400]}")
        r.run("herdr", "agent", "send-keys", slug, "down")  # trust the folder
        r.run("herdr", "agent", "send-keys", slug, "enter")
        r.run("herdr", "agent", "wait", slug, "--until", "idle", "--timeout", "60000")
    r.run("herdr", "agent", "prompt", slug, brief)
    r.run("herdr", "agent", "wait", slug, "--until", "working", "--timeout", "60000")
    print(f"builder {slug} started on {branch} in {wt}, pane {pane}")


def cleanup(r: Runner, slug: str, worktrees: Path, force: bool) -> None:
    branch, wt = branch_of(slug), worktrees / slug
    prs = open_prs(
        json.loads(
            r.read("gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state")
        )
    )
    if prs and not force:
        sys.exit(
            f"PR {', '.join(f'#{n}' for n in prs)} on {branch} is still open; --force to override"
        )
    agent = find_agent(json.loads(r.read("herdr", "agent", "list")), slug)
    tab = agent["tab_id"] if agent else find_tab(json.loads(r.read("herdr", "tab", "list")), slug)
    if tab:
        r.run("herdr", "tab", "close", tab)
    if wt.exists():
        r.run("git", "worktree", "remove", *(["--force"] if force else []), str(wt))
    r.run("git", "worktree", "prune")
    if r.read("git", "branch", "--list", branch).strip():
        r.run("git", "branch", "-D", branch)
    if _remote_has(r, branch):
        r.run("git", "push", "origin", "--delete", branch)
    print(f"builder {slug} cleaned up")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="builder.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--worktrees", type=Path, help="default: $HAT_WORKTREES or <main>/../wt/hat")
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("slug")
    s.add_argument("--brief", type=Path, required=True, help="the brief file, sent as the prompt")
    c = sub.add_parser("cleanup")
    c.add_argument("slug")
    c.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    r = Runner(args.dry_run)
    try:
        branch_of(args.slug)
    except ValueError as e:
        p.error(str(e))
    common = Path(r.read("git", "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    worktrees = args.worktrees or default_worktrees(common, os.environ)
    if args.cmd == "start":
        start(r, args.slug, args.brief.read_text().strip(), worktrees)
    else:
        cleanup(r, args.slug, worktrees, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
