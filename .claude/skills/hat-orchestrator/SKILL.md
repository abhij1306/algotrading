---
name: hat-orchestrator
description: Run the HAT build as orchestrator - start and brief builder agents in herdr tabs (max two), watch them, run the merge queue, verify and merge PRs. Use when the owner says to continue, resume or orchestrate the HAT build, or types /hat-orchestrator.
disable-model-invocation: true
---

# HAT orchestrator

1. Read `docs/agents/orchestrator.md` in full and follow it. It overrides your defaults.
2. Read `docs/agents/plan.md` (roadmap and status) and skim `docs/agents/builder-protocol.md`
   (what builders are told, so your briefs never contradict it).
3. Check you are inside herdr: `test "${HERDR_ENV:-}" = 1`. If not, tell the owner to start
   `herdr` and run `claude --permission-mode auto` inside it, then stop.
4. Rebuild state from GitHub and herdr (step 4 of "Starting on a machine"), report it to the owner
   in a few lines (open PRs, running builders, what's next), then continue the queue.
