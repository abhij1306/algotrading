"""Claude Code hook that records a builder agent's state, read by `builder.py wait` and `status`.

`builder.py install-hooks` puts it in the user's ~/.claude/settings.json for SessionStart,
UserPromptSubmit, Stop and Notification:

    python3 <checkout>/scripts/agents/agent_hook.py --root <state root> --worktrees <worktrees>

A session is a builder when `BUILDER_SLUG` is set or its cwd is under `<worktrees>/<slug>`; any
other session (the orchestrator, the owner) is left alone. The state goes to
`<root>/<slug>/state.json` (`HAT_AGENT_STATE` overrides `--root`), written atomically under a
lock. The hook never fails the agent's turn: every error is swallowed and it prints nothing.

`turn` counts the prompts the builder was sent; a background task's completion notice continues
the turn it belongs to. `state` is working, idle, needs-input (a permission dialog) or
waiting-on-background-task, and `results` maps recent turns to the line `wait` prints: the
verdict, `idle no-verdict`, `needs-input` or `background-task`. The last two are not final: the
turn goes on. In tmux, the builder's window is coloured by its state.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

STATE_FILE = "state.json"
# words of letters and digits joined by one '-', '_' or '.': a valid git ref component
SLUG = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
VERDICT = re.compile(r"READY TO MERGE #\d+ [0-9a-f]{7,40}|BLOCKED #\d+: .+|OWNER DECISION: .+")
NO_VERDICT, NEEDS_INPUT, BACKGROUND = "idle no-verdict", "needs-input", "background-task"
WORKING, IDLE = "working", "idle"
WAITING_ON_BACKGROUND = "waiting-on-background-task"
TASK_NOTICE = "<task-notification>"
KEEP_RESULTS = 20
STYLE = {WORKING: "default", NEEDS_INPUT: "fg=red", WAITING_ON_BACKGROUND: "fg=cyan"}


def initial_state() -> dict[str, Any]:
    return {"turn": 0, "state": "starting", "results": {}, "seq": 0, "updated": 0.0}


def slug_of(env: Mapping[str, str], cwd: str, worktrees: Path) -> str | None:
    """`BUILDER_SLUG`, else the first folder of cwd under the worktrees dir; None: not a builder."""
    slug = env.get("BUILDER_SLUG", "")
    if not slug and cwd:
        with contextlib.suppress(ValueError):
            parts = Path(cwd).relative_to(worktrees).parts
            slug = parts[0] if parts else ""
    return slug if SLUG.fullmatch(slug) else None


def verdict_of(message: str) -> str | None:
    """The last line of the reply that is a verdict, without list bullets, bold or backticks."""
    for line in reversed(message.splitlines()):
        line = line.strip().lstrip("●").strip().strip("*`").strip()
        if VERDICT.fullmatch(line):
            return line
    return None


def apply(
    state: Mapping[str, Any], payload: Mapping[str, Any], env: Mapping[str, str], now: float
) -> dict[str, Any] | None:
    """The state after a hook event, or None when the event changes nothing."""
    s = {**initial_state(), **state}
    results = dict(s["results"])
    turn = s["turn"]
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        s["session_at"] = now
        if payload.get("source") != "compact":  # a compaction can happen mid-turn
            s["state"] = IDLE
    elif event == "UserPromptSubmit":
        if str(payload.get("prompt", "")).lstrip().startswith(TASK_NOTICE):
            if results.get(str(turn)) == BACKGROUND:
                del results[str(turn)]
        else:
            turn += 1
            s["prompt_id"] = payload.get("prompt_id")
        s["state"] = WORKING
    elif event == "Stop":
        background = bool(payload.get("background_tasks"))
        verdict = verdict_of(str(payload.get("last_assistant_message") or ""))
        # a verdict counts only once the turn's background work is done too
        if verdict and not background:
            s["verdict"], s["verdict_turn"] = verdict, turn
        s["state"] = WAITING_ON_BACKGROUND if background else IDLE
        results[str(turn)] = BACKGROUND if background else verdict or NO_VERDICT
    elif event == "Notification" and payload.get("notification_type") == "permission_prompt":
        s["state"] = results[str(turn)] = NEEDS_INPUT
    else:  # idle_prompt and the other notices
        return None
    s["turn"] = turn
    s["results"] = {k: results[k] for k in sorted(results, key=int)[-KEEP_RESULTS:]}
    s["seq"] += 1
    s["updated"] = now
    if payload.get("session_id"):
        s["session_id"] = payload["session_id"]
    if env.get("CLAUDE_PID", "").isdigit():
        s["pid"] = int(env["CLAUDE_PID"])
    if env.get("TMUX_PANE") or env.get("HERDR_PANE_ID"):
        s["pane"] = env.get("TMUX_PANE") or env.get("HERDR_PANE_ID")
    return s


def answered(state: Mapping[str, Any], now: float) -> dict[str, Any] | None:
    """After the orchestrator answers a permission dialog: working again, or None if none was up."""
    if state.get("state") != NEEDS_INPUT:
        return None
    results = {k: v for k, v in state["results"].items() if v != NEEDS_INPUT}
    return {**state, "state": WORKING, "results": results, "seq": state["seq"] + 1, "updated": now}


def wait_answer(state: Mapping[str, Any], turn: int, since: int) -> str | None:
    """What `wait` prints for `turn`, or None to keep waiting. `since` is the state's seq when the
    wait began: a background task already running then is not news (an open dialog is)."""
    result = state["results"].get(str(turn))
    if result == BACKGROUND and turn == state["turn"] and state["seq"] <= since:
        return None
    return result


def style_of(state: Mapping[str, Any]) -> str:
    """The tmux window style for a state: green on a verdict, yellow when idle without one."""
    if state["state"] == IDLE:
        last = state["results"].get(str(state["turn"]))
        return "fg=yellow" if last in (None, NO_VERDICT) else "fg=green"
    return STYLE.get(state["state"], "default")


def read_state(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / STATE_FILE).read_text())
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def locked(folder: Path, name: str = ".lock") -> Iterator[None]:
    """An exclusive lock on `folder/name`: `.lock` guards the state, `.send.lock` a send."""
    # the folder is the operator's state root plus a slug restricted to SLUG (no "/" or "..")
    folder.mkdir(parents=True, exist_ok=True)  # NOSONAR
    with open(folder / name, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update(folder: Path, change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict | None:
    """Apply `change` to the folder's state under its lock; write it atomically if it changed."""
    with locked(folder):
        new = change(read_state(folder) or initial_state())
        if new is not None:
            tmp = folder / f".{STATE_FILE}.{os.getpid()}"
            tmp.write_text(json.dumps(new, indent=1))
            os.replace(tmp, folder / STATE_FILE)
        return new


def _run(argv: list[str] | None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--worktrees", type=Path, required=True)
    args = p.parse_args(argv)
    payload = json.load(sys.stdin)
    env = dict(os.environ)
    slug = slug_of(env, str(payload.get("cwd", "")), args.worktrees)
    if not slug:
        return
    root = Path(env["HAT_AGENT_STATE"]) if env.get("HAT_AGENT_STATE") else args.root
    new = update(root / slug, lambda s: apply(s, payload, env, time.time()))
    if new is not None and env.get("TMUX_PANE"):
        style = ("window-status-style", style_of(new))
        # a fixed tmux command; the pane id is tmux's own
        subprocess.run(  # NOSONAR
            ["tmux", "set-option", "-w", "-t", env["TMUX_PANE"], *style],  # noqa: S607
            capture_output=True,
            timeout=3,
            check=False,
        )


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(BaseException), contextlib.redirect_stderr(io.StringIO()):
        _run(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
