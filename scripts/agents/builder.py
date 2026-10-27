"""Start, prompt, wait on, list and clean up builder agents, in tmux or herdr.

    python scripts/agents/builder.py start <slug> --brief FILE
    python scripts/agents/builder.py send <slug> TEXT | --file FILE
    python scripts/agents/builder.py wait <slug> [--turn N] [--timeout 45m]
    python scripts/agents/builder.py keys <slug> KEY...
    python scripts/agents/builder.py status
    python scripts/agents/builder.py stop <slug>
    python scripts/agents/builder.py cleanup <slug> [--force]
    python scripts/agents/builder.py install-hooks [--settings FILE]

The backend is herdr inside herdr (`HERDR_ENV=1`), else tmux inside tmux (`$TMUX`); `--backend`
overrides. A builder's state comes from the Claude Code hook `agent_hook.py` (installed once by
`install-hooks`), in `<state>/<slug>/` (`<state>` is `HAT_AGENT_STATE`, else `agents` beside the
main checkout), never from the screen.

`start` fetches, adds the worktree `<worktrees>/<slug>` on `hat/<slug>` (from `origin/main`, or
`origin/hat/<slug>` when the branch exists; an existing worktree is reused), opens a tmux window
or herdr tab named after the slug running `claude --permission-mode auto`, accepts a first-start
folder-trust prompt, sends the brief and waits for the agent to take it. It prints the slug, pane
and turn.

`send` pastes a prompt and prints the turn it opens. `wait` returns when that turn (default: the
latest) ends or stops on a permission dialog, at once if it already did, and prints one line: the
verdict (`READY TO MERGE #N <sha>`, `BLOCKED #N: ...`, `OWNER DECISION: ...`), `idle no-verdict`,
`needs-input` (a permission dialog: answer it with `keys`) or `background-task` (the turn stopped
with background work running: wait again). At the timeout it prints `still-working turn N`, or
`crashed` when the agent's process is gone. `status` prints one line per builder. `stop` closes
the window or tab, ending the agent (to restart it fresh with `start`, which reuses the worktree).

`cleanup` refuses while the branch's PR is open (`--force` overrides, and also removes a dirty
worktree), then closes the window or tab (ending the agent), removes the worktree, the branch
(local and on origin) and the state folder. `<worktrees>` is `--worktrees`, else `HAT_WORKTREES`,
else `wt/hat` beside the main checkout. `--dry-run` prints the commands instead of running them.

`install-hooks` adds the hook to the user's Claude Code settings for SessionStart,
UserPromptSubmit, Stop and Notification; it is idempotent and changes no other entry.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_hook as hook  # noqa: E402

BRANCH_PREFIX = "hat/"
CLAUDE = ("claude", "--permission-mode", "auto")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification")
HOOK_SCRIPT = "agent_hook.py"
TRUST_PROMPT = "Yes, I trust this folder"
START_TIMEOUT = 90.0
PROMPT_TIMEOUT = 60.0
POLL = 2.0
TMUX, HERDR = "tmux", "herdr"
T = TypeVar("T")


def branch_of(slug: str) -> str:
    if not hook.SLUG.fullmatch(slug) or slug.endswith(".lock"):
        raise ValueError(f"bad slug {slug!r}: words of letters and digits joined by '-', '_', '.'")
    return BRANCH_PREFIX + slug


def default_worktrees(common_git_dir: Path, env: Mapping[str, str]) -> Path:
    """`HAT_WORKTREES`, else `wt/hat` beside the main checkout (the parent of its .git)."""
    if env.get("HAT_WORKTREES"):
        return Path(env["HAT_WORKTREES"])
    return common_git_dir.resolve().parent.parent / "wt" / "hat"


def default_state_root(common_git_dir: Path, env: Mapping[str, str]) -> Path:
    """`HAT_AGENT_STATE`, else `agents` beside the main checkout."""
    if env.get("HAT_AGENT_STATE"):
        return Path(env["HAT_AGENT_STATE"])
    return common_git_dir.resolve().parent.parent / "agents"


def choose_backend(env: Mapping[str, str], override: str | None) -> str:
    if override:
        return override
    if env.get("HERDR_ENV") == "1":
        return HERDR
    if env.get("TMUX"):
        return TMUX
    raise ValueError("not inside herdr or tmux: start one and run the orchestrator inside it")


def parse_duration(text: str) -> float:
    """Seconds in `90`, `90s`, `45m` or `2h`."""
    m = re.fullmatch(r"(\d+)([smh]?)", text.strip())
    if not m:
        raise ValueError(f"bad duration {text!r}: like 90s, 45m or 2h")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def hook_command(checkout: Path, root: Path, worktrees: Path) -> str:
    """The hook's command line, every path shell-quoted (the operator's own checkout and dirs)."""
    script = checkout / "scripts" / "agents" / HOOK_SCRIPT
    args = ["python3", str(script), "--root", str(root), "--worktrees", str(worktrees)]
    return shlex.join(args)  # NOSONAR


def with_hooks(settings: Mapping[str, Any], command: str) -> dict[str, Any]:
    """The settings with the agent hook on each event: an existing agent hook entry is updated
    in place, a missing one appended; every other entry is kept as it was."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        ours = [
            h
            for g in groups
            for h in g.get("hooks", [])
            if HOOK_SCRIPT in str(h.get("command", ""))
        ]
        for h in ours:
            h["command"] = command
        if not ours:
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return out


def pane_of(tab_create: Mapping[str, Any]) -> str:
    """The new tab's pane id from `herdr tab create`'s JSON."""
    return tab_create["result"]["root_pane"]["pane_id"]


def find_agent(agent_list: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    return next((a for a in agent_list["result"]["agents"] if a.get("name") == name), None)


def find_tab(tab_list: Mapping[str, Any], label: str) -> str | None:
    return next((t["tab_id"] for t in tab_list["result"]["tabs"] if t.get("label") == label), None)


def open_prs(pr_list: Sequence[Mapping[str, Any]]) -> list[int]:
    return [p["number"] for p in pr_list if p.get("state") == "OPEN"]


def status_line(slug: str, state: Mapping[str, Any], now: float) -> str:
    age = int(now - state.get("updated", 0)) // 60
    verdict = state.get("verdict") or "-"
    return f"{slug} {state['state']} turn {state['turn']} age {age}m last verdict: {verdict}"


class Runner:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def run(
        self, *cmd: str, check: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a command (print it on a dry run); a check failure exits with its stderr."""
        if self.dry_run:
            print("+", shlex.join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # fixed git/gh/herdr/tmux commands (a taint-analysis false positive): the one operator
        # input, the slug, is restricted by branch_of to [A-Za-z0-9._-], never leading with "-"
        done = subprocess.run(  # NOSONAR
            cmd, input=stdin, capture_output=True, text=True, check=False
        )
        if check and done.returncode:
            sys.exit(f"{shlex.join(cmd[:4])} failed: {(done.stderr or done.stdout).strip()[:400]}")
        return done

    def read(self, *cmd: str) -> str:
        """Run a read-only command even on a dry run."""
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # NOSONAR


class Tmux:
    """A tmux window named after the slug; prompts go through a named buffer per slug."""

    def __init__(self, r: Runner) -> None:
        self.r = r

    def open(self, slug: str, wt: Path, root: Path) -> str:
        new = self.r.run(
            "tmux", "new-window", "-d", "-n", slug, "-e", f"BUILDER_SLUG={slug}",
            "-e", f"HAT_AGENT_STATE={root}", "-c", str(wt), "-P", "-F", "#{pane_id}",
            shlex.join(CLAUDE),
        )  # fmt: skip
        return new.stdout.strip() or "<pane>"

    def trust_prompt(self, pane: str) -> bool:
        shown = self.r.run("tmux", "capture-pane", "-p", "-t", pane, check=False).stdout
        return TRUST_PROMPT in shown

    def accept_trust(self, slug: str, pane: str) -> None:
        self.keys(slug, pane, ["Down"])
        self.keys(slug, pane, ["Enter"])

    def paste(self, slug: str, pane: str, text: str) -> None:
        self.r.run("tmux", "load-buffer", "-b", slug, "-", stdin=text)
        self.r.run("tmux", "paste-buffer", "-p", "-d", "-b", slug, "-t", pane)
        if not self.r.dry_run:
            time.sleep(1)  # let the paste land before Enter submits it
        self.keys(slug, pane, ["Enter"])

    def keys(self, slug: str, pane: str, keys: Sequence[str]) -> None:
        self.r.run("tmux", "send-keys", "-t", pane, *keys)

    def has_pane(self, slug: str, pane: str) -> bool:
        return pane in self.r.read("tmux", "list-panes", "-a", "-F", "#{pane_id}").split()

    def close(self, slug: str, pane: str | None) -> None:
        if pane and self.has_pane(slug, pane):
            self.r.run("tmux", "kill-window", "-t", pane)


class Herdr:
    """A herdr tab labelled with the slug, its agent named after the slug."""

    def __init__(self, r: Runner) -> None:
        self.r = r
        self._untrusted = False

    def open(self, slug: str, wt: Path, root: Path) -> str:
        # the agent gets the pane's environment: the hook finds it by its cwd and its own --root
        tab = self.r.run("herdr", "tab", "create", "--cwd", str(wt), "--label", slug, "--no-focus")
        pane = "<pane>" if self.r.dry_run else pane_of(json.loads(tab.stdout))
        started = self.r.run(
            "herdr", "agent", "start", slug, "--kind", "claude", "--pane", pane,
            "--", *CLAUDE[1:], check=False,
        )  # fmt: skip
        out = started.stdout + started.stderr
        if started.returncode and "agent_not_ready" not in out:
            sys.exit(f"agent start failed: {out.strip()[:400]}")
        self._untrusted = bool(started.returncode)
        return pane

    def trust_prompt(self, pane: str) -> bool:
        untrusted, self._untrusted = self._untrusted, False  # agent start said so, once
        return untrusted

    def accept_trust(self, slug: str, pane: str) -> None:
        self.keys(slug, pane, ["down"])
        self.keys(slug, pane, ["enter"])

    def paste(self, slug: str, pane: str, text: str) -> None:
        self.r.run("herdr", "agent", "prompt", slug, text)

    def keys(self, slug: str, pane: str, keys: Sequence[str]) -> None:
        for key in keys:
            self.r.run("herdr", "agent", "send-keys", slug, key)

    def has_pane(self, slug: str, pane: str) -> bool:
        return find_agent(json.loads(self.r.read("herdr", "agent", "list")), slug) is not None

    def close(self, slug: str, pane: str | None) -> None:
        agent = find_agent(json.loads(self.r.read("herdr", "agent", "list")), slug)
        tab = (
            agent["tab_id"]
            if agent
            else find_tab(json.loads(self.r.read("herdr", "tab", "list")), slug)
        )
        if tab:
            self.r.run("herdr", "tab", "close", tab)


Backend = Tmux | Herdr


def _remote_has(r: Runner, branch: str) -> bool:
    return bool(r.read("git", "ls-remote", "--heads", "origin", branch).strip())


def _state(folder: Path) -> dict[str, Any]:
    return {**hook.initial_state(), **(hook.read_state(folder) or {})}


def _poll(
    until: Callable[[], T], timeout: float, tick: Callable[[], None] | None = None
) -> T | None:
    """`until()`'s first truthy value within the timeout, checked every POLL seconds, else None."""
    deadline = time.monotonic() + timeout
    while True:
        if (got := until()) or time.monotonic() >= deadline:
            return got or None
        if tick:
            tick()
        time.sleep(POLL)


def _submit(b: Backend, slug: str, pane: str, folder: Path, text: str) -> int | None:
    """Paste a prompt and return the turn it opened, or None if the agent hasn't taken it yet."""
    before = _state(folder)["turn"]
    b.paste(slug, pane, text)
    if b.r.dry_run:
        return before + 1
    return _poll(lambda: (t := _state(folder)["turn"]) > before and t, PROMPT_TIMEOUT)


def start(b: Backend, slug: str, brief: str, worktrees: Path, folder: Path) -> None:
    r = b.r
    branch, wt = branch_of(slug), worktrees / slug
    r.run("git", "fetch", "origin")
    if wt.exists():
        on = r.read("git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").strip()
        if on != branch:
            sys.exit(f"{wt} exists but is on {on or 'no branch'}, not {branch}")
        print(f"reusing {wt}")
    elif _remote_has(r, branch):
        r.run("git", "worktree", "add", "-B", branch, str(wt), f"origin/{branch}")
    else:
        r.run("git", "worktree", "add", "-b", branch, str(wt), "origin/main")
    began = time.time()
    pane = b.open(slug, wt, folder.parent)
    if not r.dry_run:

        def trust() -> None:
            if b.trust_prompt(pane):
                b.accept_trust(slug, pane)

        trust()
        started = _poll(lambda: _state(folder).get("session_at", 0) >= began, START_TIMEOUT, trust)
        if not started:
            sys.exit(f"{slug}: no session start in {START_TIMEOUT:.0f}s (are the hooks installed?)")
        time.sleep(1)  # the input box is ready a moment after SessionStart
    turn = _submit(b, slug, pane, folder, brief)
    if turn is None:
        sys.exit(f"{slug}: the agent didn't take the brief; look at pane {pane}")
    print(f"builder {slug} pane {pane} turn {turn} on {branch} in {wt}")


def _pane(b: Backend, slug: str, folder: Path) -> str:
    """The pane the hook recorded; herdr addresses the agent by name and needs none."""
    pane = _state(folder).get("pane") or ""
    if not pane and isinstance(b, Tmux):
        sys.exit(f"{slug}: no pane recorded; is it running?")
    return pane


def send(b: Backend, slug: str, folder: Path, text: str) -> None:
    pane = _pane(b, slug, folder)
    turn = _submit(b, slug, pane, folder, text)
    print(f"turn {turn}" if turn else f"queued: {slug} is still on turn {_state(folder)['turn']}")


def wait(b: Backend, slug: str, folder: Path, turn: int | None, timeout: float) -> None:
    first = hook.read_state(folder) or sys.exit(f"{slug}: no state in {folder}")
    turn = turn or first["turn"]
    since = first["seq"]
    answer = _poll(lambda: hook.wait_answer(_state(folder), turn, since), timeout)
    if answer:
        print(answer)
        return
    s = _state(folder)
    print(f"still-working turn {s['turn']}" if _alive(b, slug, s) else "crashed")


def _alive(b: Backend, slug: str, s: Mapping[str, Any]) -> bool:
    if s.get("pid"):
        try:
            os.kill(s["pid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    return b.has_pane(slug, s.get("pane") or "")


def keys(b: Backend, slug: str, folder: Path, keyseq: Sequence[str]) -> None:
    b.keys(slug, _pane(b, slug, folder), keyseq)
    if not b.r.dry_run:
        hook.update(folder, lambda s: hook.answered(s, time.time()))


def status(root: Path) -> None:
    now = time.time()
    folders = sorted(p for p in root.glob("*") if (p / hook.STATE_FILE).exists())
    for folder in folders:
        print(status_line(folder.name, _state(folder), now))
    if not folders:
        print(f"no builders in {root}")


def cleanup(b: Backend, slug: str, worktrees: Path, folder: Path, force: bool) -> None:
    r = b.r
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
    b.close(slug, _state(folder).get("pane"))
    if wt.exists():
        r.run("git", "worktree", "remove", *(["--force"] if force else []), str(wt))
    r.run("git", "worktree", "prune")
    if r.read("git", "branch", "--list", branch).strip():
        r.run("git", "branch", "-D", branch)
    if _remote_has(r, branch):
        r.run("git", "push", "origin", "--delete", branch)
    if folder.exists() and r.dry_run:
        print("+ rm -r", folder)
    elif folder.exists():
        shutil.rmtree(folder)
    print(f"builder {slug} cleaned up")


def install_hooks(settings_file: Path, command: str, dry_run: bool) -> None:
    # settings_file is the operator's own Claude Code settings (--settings, default ~/.claude)
    current = json.loads(settings_file.read_text()) if settings_file.exists() else {}  # NOSONAR
    new = with_hooks(current, command)
    if new == current:
        print(f"hooks already installed in {settings_file}")
        return
    if dry_run:
        print(json.dumps(new["hooks"], indent=2))
        return
    tmp = settings_file.with_name(f".{settings_file.name}.tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n")  # NOSONAR
    os.replace(tmp, settings_file)  # NOSONAR
    print(f"hooks installed in {settings_file} for {', '.join(HOOK_EVENTS)}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="builder.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--worktrees", type=Path, help="default: $HAT_WORKTREES or <main>/../wt/hat")
    p.add_argument("--backend", choices=(TMUX, HERDR), help="default: from HERDR_ENV or $TMUX")
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("slug")
    s.add_argument("--brief", type=Path, required=True, help="the brief file, sent as the prompt")
    s = sub.add_parser("send")
    s.add_argument("slug")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("text", nargs="?")
    g.add_argument("--file", type=Path)
    s = sub.add_parser("wait")
    s.add_argument("slug")
    s.add_argument("--turn", type=int, help="default: the latest turn")
    s.add_argument("--timeout", type=parse_duration, default="45m")
    s = sub.add_parser("keys", help="send keys (tmux or herdr names) to answer a dialog")
    s.add_argument("slug")
    s.add_argument("key", nargs="+")
    sub.add_parser("status")
    s = sub.add_parser("stop")
    s.add_argument("slug")
    s = sub.add_parser("cleanup")
    s.add_argument("slug")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("install-hooks")
    s.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)
    r = Runner(args.dry_run)
    slug = getattr(args, "slug", None)
    if slug is not None:
        try:
            branch_of(slug)
        except ValueError as e:
            p.error(str(e))
    common = Path(r.read("git", "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    worktrees = args.worktrees or default_worktrees(common, os.environ)
    root = default_state_root(common, os.environ)
    if args.cmd == "install-hooks":
        command = hook_command(common.resolve().parent, root, worktrees)
        install_hooks(args.settings, command, args.dry_run)
        return 0
    if args.cmd == "status":
        status(root)
        return 0
    try:
        kind = choose_backend(os.environ, args.backend)
    except ValueError as e:
        p.error(str(e))
    b: Backend = Tmux(r) if kind == TMUX else Herdr(r)
    folder = root / slug
    if args.cmd == "start":
        start(b, slug, args.brief.read_text().strip(), worktrees, folder)
    elif args.cmd == "send":
        send(b, slug, folder, args.file.read_text().strip() if args.file else args.text)
    elif args.cmd == "wait":
        wait(b, slug, folder, args.turn, args.timeout)
    elif args.cmd == "keys":
        keys(b, slug, folder, args.key)
    elif args.cmd == "stop":
        b.close(slug, _state(folder).get("pane"))
    else:
        cleanup(b, slug, worktrees, folder, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
