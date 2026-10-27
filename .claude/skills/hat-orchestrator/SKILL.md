---
name: hat-orchestrator
description: Run the HAT build as orchestrator - start and brief builder agents in tmux windows or herdr tabs (max two), watch them, run the merge queue, verify and merge PRs. Use when the owner says to continue, resume or orchestrate the HAT build, or types /hat-orchestrator.
disable-model-invocation: true
---

# HAT orchestrator

1. Read `docs/agents/orchestrator.md` in full and follow it. It overrides your defaults.
2. Read `docs/agents/builder-protocol.md` (what builders are told, so your briefs never
   contradict it) and the owner's handoff file for the queue.
3. Check you are inside tmux or herdr: `test -n "${TMUX:-}" -o "${HERDR_ENV:-}" = 1`. If not,
   tell the owner to start `tmux` (or `herdr`) and run `claude --permission-mode auto` inside
   it, then stop. Check the builder hooks are installed: `python scripts/agents/builder.py
   install-hooks --dry-run` says "already installed"; if not, run it without `--dry-run`.
4. Rebuild state from GitHub and `builder.py status` ("Start or resume"), report it to the owner
   in a few lines (open PRs, running builders, what's next), then continue the queue.
