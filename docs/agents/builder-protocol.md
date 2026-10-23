# HAT builder protocol

You build one PR, in your own git worktree and herdr tab, and take it to merge-ready. The
orchestrator merges. Your brief gives the goal, scope and follow-ups. Read `CLAUDE.md`, the
canonical docs it lists (`docs/architecture.md`, `docs/invariants.md`, `docs/decisions.md`), and
`.claude/skills/trading-safety/SKILL.md` for anything touching orders, risk, positions, brokers or
execution.

## Docs rule

Code is the source of truth; docs are a guide, not something maintained one-to-one with the code.
Once a plan or spec is implemented, delete it. Only the canonical docs are kept up to date --
architecture, invariants, CLAUDE.md, decisions -- and they stay compact.

## Owner rules

- Never push to or rebase `main`, never merge, never touch another builder's branch.
- Never call real Fyers, NSE or Telegram endpoints, never place orders, never read `.env` or
  credential files.
- Never ask CodeAnt to re-review, in any form. CodeAnt Nitpicks need no reply, but a real bug is
  fixed in this PR wherever it was spotted (a Nitpick, your own review, a test); never defer one.
- No example-based unit tests for HAT code (see Verification).
- **Legacy is debt.** Where old code or tests conflict with the invariants and decisions, change
  or delete them; no shims. List it under "Legacy changes" in the PR.
- If the brief is wrong or blocked, pick the option most faithful to `docs/invariants.md` and
  `docs/decisions.md`, list it under "Deviations", and keep going. Don't stop to ask about anything
  reversible.

## Done means

`READY TO MERGE #<n> <head sha>` as the last line of your turn, and it is true at that sha:

1. The PR does what its brief says and keeps the invariants it touches, verified as below.
2. Every CI check passes and the SonarCloud quality gate is OK.
3. Every real bug found by you, Sonar or CodeAnt is fixed. A Sonar or CodeAnt finding (inline
   comment or PR Risk) you judge not to be a real bug gets a reply saying why. Cosmetic findings
   don't block.

If you can't get there, end with `BLOCKED #<n>: <reason and what you tried>`.

## Verification

Prove the invariants on the real code, following
https://raw.githubusercontent.com/cursor/plugins/main/pstack/skills/create-formal-verification-skill/SKILL.md:

- ordering, concurrency and state machines: a TLA+ model in `verification/tla/` with broken `.cfg`
  variants next to it;
- every-input promises: Hypothesis properties under `backend/tests/hat/properties/`;
- database invariants: `@pytest.mark.postgres` checks.

Each check must be seen failing on a deliberately broken variant before you trust it; record the
breaks in the PR and the area's `.claude/skills/verify-<area>/SKILL.md`. A property the code
breaks is a finding: fix the code, never weaken the property.

## How to work fast

- **One push, then one fix round.** Open the PR only when the work is complete and checked
  locally. Before that first push, run a fresh-context review of the whole diff (`/code-review`)
  and fix what it finds; that is where CodeAnt's real bugs get caught early. After the push, wait
  until CI, Sonar and CodeAnt have all finished, then fix everything in one commit.
- **Pre-push check**, from the worktree root: `ruff format --check .`, `ruff check .`, then from
  `backend/` the test files you touched plus `tests/hat/conformance`, `-m postgres` files if you
  touched schema, and `pnpm check` if you touched the frontend. CI runs everything else.
  Last, `python backend/tests/hat/conformance/hat_conf_skills.py --stale` names the `verify-*`
  skills whose covered files you changed: make sure each one's Properties and Coverage still
  describe the code, then record it with `--refresh`. Don't rerun its whole Run command; CI does.
- **Never run full suites locally.** No full `pytest`, no `CI=1` property runs, no run of every
  TLC model, not before the push, not after a rebase. CI runs all of them on every push. Run TLC
  only for a model you changed, and a break only for a check you added or changed. After a
  rebase, rerun the pre-push check above and nothing more.
- **Tests while iterating:** run the one test you are working on, default Hypothesis profile.
- **Context:** keep `HANDOFF.md` in your worktree current (goal, done, next, open findings) so a
  compaction or a fresh session resumes from it instead of re-reading. Send code
  exploration to subagents that return file pointers and conclusions, not file dumps. Never read
  your own session transcript.
- **Waiting:** wait on CI with `gh pr checks <n> --watch` under `run_in_background`; never
  foreground `sleep`, never `pgrep -f` loops (they match themselves and never end).
- **Rebase** only when the orchestrator says you're next to merge: rebase onto `origin/main`,
  keep both sides in shared files (`docs/invariants.md`, verify skills), rerun the pre-push
  check (including the `--stale` step), `git push --force-with-lease`, report READY again.

## PR

- Branch `hat/<slug>`. Conventional commits; end each commit message with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Body: summary, verification and breaks, deviations, legacy changes, follow-ups. End
  with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Bookkeeping: delete any plan or spec you implemented.
- Sonar (public, no token): gate
  `https://sonarcloud.io/api/qualitygates/project_status?projectKey=abhij1306_algotrading&pullRequest=<n>`,
  issues `https://sonarcloud.io/api/issues/search?componentKeys=abhij1306_algotrading&pullRequest=<n>&resolved=false`.
- CodeAnt: inline comments `gh api repos/abhij1306/algotrading/pulls/<n>/comments`, PR Risk in
  `gh api repos/abhij1306/algotrading/issues/<n>/comments`. Reply `Re CodeAnt <what>: fixed in
  <sha>. <how>` or a short reason. If it hasn't reviewed 30 minutes after opening, go on without it.

## Report

Above the final line: PR URL, what was built, verification and breaks, how each review
finding was handled, deviations, follow-ups. Keep it short.
