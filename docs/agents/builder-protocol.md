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
2. Every CI check passes and the SonarCloud quality gate is OK.
3. Every real bug found by you, Sonar or CodeAnt is fixed. A Sonar or CodeAnt finding (inline
   comment or PR Risk) you judge not to be a real bug gets a reply saying why. Cosmetic findings
   don't block.

If you can't get there, end with `BLOCKED #<n>: <reason and what you tried>`.

## Verification

Implementation and real verification come first; testing is not proof.

- Read your diff against the invariants it touches (`trading-safety` for the order path).
- Run the change for real: the affected job, CLI or route against real data (`HAT_DATA_ROOT`,
  the local database read-only), and a real backtest on real data for engine changes. Paste the
  commands and the key output in the PR under "Verification".
- Tests only where cheap and meaningful: small pure functions at their seams (money maths,
  parsers, state rules). No new TLA+ models, Hypothesis suites or deliberate-break records
  unless the brief asks. Never a test that runs a backtest, the pipeline or a full database on
  generated data. If an existing test fights a correct change, delete or fix the test.

## How to work fast

- **One push, then one fix round.** Open the PR only when the work is complete and verified.
  After the push, wait
  until CI, Sonar and CodeAnt have all finished, then fix everything in one commit.
- **Pre-push check**, from the worktree root: `ruff format --check .`, `ruff check .`, then from
  `backend/` the test files you touched plus `tests/hat/conformance`, and `pnpm check` if you
  touched the frontend. CI runs everything else.
- **Never run full suites locally.** No full `pytest`, no `CI=1` property runs, no run of every
  TLC model. CI runs them. After a rebase, rerun the pre-push check above and nothing more.
- **Tests while iterating:** run the one test you are working on, default Hypothesis profile.
- **Context:** keep `HANDOFF.md` in your worktree current (goal, done, next, open findings) so a
  compaction or a fresh session resumes from it instead of re-reading. Send code
  exploration to subagents that return file pointers and conclusions, not file dumps. Never read
  your own session transcript.
- **Waiting:** wait on CI with `gh pr checks <n> --watch` under `run_in_background`; never
  foreground `sleep`, never `pgrep -f` loops (they match themselves and never end).
- **Rebase** only when the orchestrator says you're next to merge: rebase onto `origin/main`,
  keep both sides in shared files, rerun the pre-push check, `git push --force-with-lease`, report READY again.

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
  <sha>. <how>` or a short reason. If it hasn't reviewed 30 minutes after opening, go on without it.

## Report

Above the final line: PR URL, what was built, verification, how each review
finding was handled, deviations, follow-ups. Keep it short.
