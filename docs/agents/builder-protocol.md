# HAT builder protocol

You build one PR, in your own git worktree and tmux window or herdr tab, and take it to
merge-ready. The
orchestrator merges. Your brief gives the goal, scope and follow-ups. Read `CLAUDE.md`, the
canonical docs it lists (its docs rule applies to you), and
`.claude/skills/trading-safety/SKILL.md` for anything touching orders, risk, positions, brokers or
execution.

## Owner rules

- Never push to or rebase `main`, never merge, never touch another builder's branch.
- Never call real Fyers, NSE or Telegram endpoints, never place orders, never read `.env` or
  credential files.
- Never ask CodeAnt to re-review, in any form. CodeAnt Nitpicks need no reply, but a real bug is
  fixed in this PR wherever it was spotted (a Nitpick, your own review, a test); never defer one.
- No tests that run a backtest, the pipeline or a full database on generated data (see
  Verification).
- **Legacy is debt.** Where old code or tests conflict with the invariants and decisions, change
  or delete them; no shims. List it under "Legacy changes" in the PR.
- If the brief is wrong or blocked, pick the option most faithful to `docs/invariants.md` and
  `docs/decisions.md`, list it under "Deviations", and keep going. Don't stop to ask about anything
  reversible.

## Done means

`READY TO MERGE #<n> <head sha>` as the last line of your turn, and it is true at that sha:

1. The PR does what its brief says and keeps the invariants it touches, verified as below.
2. Every required check passes (`gh pr checks <n> --required`: ci.yml and SonarCloud) and the
   SonarCloud quality gate is OK. The TLA workflow (4-6 min) is not required: don't wait for it;
   the orchestrator checks it at merge and sends you any failure. Sonar code smells (naming,
   literals, complexity, test style) need neither a fix nor a reply; only the gate and real bugs
   count. CI still running is never a reason for BLOCKED: wait for it.
   Don't wait for CodeAnt: report
   READY once CI and Sonar are green, and fix any CodeAnt finding that arrives before merge.
3. Every real bug found by you, Sonar or CodeAnt is fixed. A Sonar or CodeAnt finding (inline
   comment or PR Risk) you judge not to be a real bug gets a reply saying why. Cosmetic findings
   don't block.

If you can't get there, end with `BLOCKED #<n>: <reason and what you tried>`.

## Verification

Implementation and real verification come first; testing is not proof.

- Read your diff against the invariants it touches (`trading-safety` for the order path).
- Run the change for real: the affected job, CLI or route against real data (`HAT_DATA_ROOT`,
  the local database read-only). Paste the commands and the key output in the PR under
  "Verification".
- Size it to the change, and run it **once**, after the code is final. Never run a backtest as PR
  verification (owner, 2026-10-23: a waste of time). A pure refactor needs no run: CI, ruff and the
  touched plain tests are enough. A behaviour change runs the affected job, CLI or route once. Don't
  re-run after a review fix that doesn't change behaviour.
- Tests only where cheap and meaningful: small pure functions at their seams (money maths,
  parsers, state rules). No new TLA+ models, Hypothesis suites or deliberate-break records
  unless the brief asks. Never a test that runs a backtest, the pipeline or a full database on
  generated data. If an existing test fights a correct change, delete or fix the test.

## How to work fast

- **One push, then one fix round.** Open the PR only when the work is complete and verified.
  After the push, wait
  until CI and Sonar have finished, then fix everything (with any CodeAnt findings already in) in
  one commit.
- **Pre-push check**, from the worktree root: `ruff format --check .`, `ruff check .`, then from
  `backend/` the test files you touched plus `tests/hat/conformance`, and `pnpm check` if you
  touched the frontend. CI runs everything else; the TLC models run in `tla.yml` only when a model
  or the code its actions map to changes.
- **Never run full suites locally.** No full `pytest`, no `CI=1` property runs, no run of every
  TLC model. CI runs them. After a rebase, rerun the pre-push check above and nothing more.
- **Tests while iterating:** run the one test you are working on, default Hypothesis profile.
- **Context:** keep `HANDOFF.md` in your worktree current (goal, done, next, open findings) so a
  compaction or a fresh session resumes from it instead of re-reading. Send code
  exploration to subagents that return file pointers and conclusions, not file dumps. Never read
  your own session transcript.
- **Waiting:** start the long command itself (a CI watch `gh pr checks <n> --watch --required`, a backtest,
  a verification run) with `run_in_background` and wait for its completion notification. Never
  `until`/`while ... sleep` loops or `pgrep -f` (it matches itself and never ends): each one
  runs to the 10-minute tool timeout, and they cost 40 builder minutes on 2026-10-23.
- **Rebase** once, just before reporting READY: if `origin/main` moved, rebase onto it, keep
  both sides in shared files, rerun the pre-push check, `git push --force-with-lease`, wait for
  CI. Not on every merge to main while you work: each rebase costs a CI run.

## PR

- Branch `hat/<slug>`. Conventional commits; end each commit message with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Body: summary, verification (real-run evidence), deviations, legacy changes, follow-ups. End
  with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Bookkeeping: delete any plan or spec you implemented.
- Sonar (public, no token): gate
  `https://sonarcloud.io/api/qualitygates/project_status?projectKey=abhij1306_algotrading&pullRequest=<n>`,
  issues `https://sonarcloud.io/api/issues/search?componentKeys=abhij1306_algotrading&pullRequest=<n>&resolved=false`.
- CodeAnt: inline comments `gh api repos/abhij1306/algotrading/pulls/<n>/comments`, PR Risk in
  `gh api repos/abhij1306/algotrading/issues/<n>/comments`. Reply `Re CodeAnt <what>: fixed in
  <sha>. <how>` or a short reason. Never wait for it; a finding that arrives after READY and
  before merge is fixed then, and READY reported again.

## Report

The orchestrator reads the verdict from your turn's final message (a Claude Code hook), so it
must be the last line of that message, not an earlier one, and a turn with no verdict reads as
stalled. Above the final line: PR URL, what was built, verification, how each review
finding was handled, deviations, follow-ups. Keep it short.
