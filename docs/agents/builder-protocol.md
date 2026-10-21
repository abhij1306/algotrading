# HAT builder protocol

Every builder agent follows this file. You are a Claude agent running in your own herdr tab, in a
git worktree created for your PR by the orchestrator (see `orchestrator.md`). Worktrees sit in a
`wt/` folder next to the main clone: `../wt/<branch>` from the clone. The machine may be Windows
(herdr, Git Bash) or Linux; use commands that work on the one you're on. The PR roadmap is `docs/agents/plan.md`; the specs are `docs/specs/hat/`.

## HARD RULES (owner). Read these first; they override anything below
1. **No full test suites locally.** Run only the tests your diff touches, plus `tests/hat/conformance`. CI runs the full suite.
2. **No example-based unit tests for HAT code.** Only properties (Hypothesis), TLA+ models, DB checks and CONF checks. A fixed example is allowed only as an `@example` on a property, or where the spec asks for a golden value.
3. **Never ask CodeAnt to re-review**, in any form.
4. **Batch your pushes.** Push the PR once. Then wait until CodeAnt has finished (its "Review Status" comment shows `"done":true` for your head commit) and Sonar has analysed that commit, and fix everything in **one** commit per review round: Sonar issues, CodeAnt inline comments, CodeAnt Nitpicks and PR Risk. Don't push a fix per finding; every push restarts CI and re-queues CodeAnt.
5. **Don't rebase until the orchestrator says it's your turn to merge.** If `main` moves while you're working, ignore it. When you're ready, report "ready to merge"; the orchestrator tells you if a rebase is needed.
6. Builders never merge. Never push to `main`.
7. **Never wait with `pgrep -f`** in any form: it matches the waiting shell itself and hangs forever. To wait on CI, run `timeout 590 gh pr checks <n> --watch --interval 30` with `run_in_background`. A PR that conflicts with main gets no CI at all; if `gh pr checks` shows only CodeRabbit and Sonar, report it.

## Setup

- Main clone: the `algotrading` checkout the orchestrator runs in (GitHub `abhij1306/algotrading`, default branch `main`). `gh` is logged in as the owner.
- You work in your own git worktree so parallel builders never collide;
  the orchestrator creates it before starting you.
  Branch name: `hat/prNN-<slug>` (e.g. `hat/pr02-foundation`).
- Python: the main clone's `venv` (`venv/bin/activate`, or `venv\Scripts\activate` on Windows; CPython 3.14, deps from `backend/requirements-ci.lock`). If your PR adds a dependency, add it to the requirements and the lock the way the repo does, and install it into this shared venv.
- Node 26 on PATH, pnpm via `npx pnpm@12.14.0` (or `corepack`). Run `pnpm install --frozen-lockfile` in your worktree's `frontend/` before frontend checks.
- Postgres for `-m postgres` tests: container `hatpg` on localhost:5432, env `DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test` (no password), same as CI.
- Read the repo's `CLAUDE.md` and, for anything touching orders, risk, positions, brokers or execution, `.claude/skills/trading-safety/SKILL.md`. Follow both.

- Your brief lists items earlier PRs left to yours. Do those too.

## Agents and subagents

- You run on the main model. You may use Sonnet subagents (`model: "sonnet"`) for exploration or
  mechanical building; you own and check what they produce.
- Take your PR all the way to green: build, push, CI, every Sonar and CodeAnt point, then report
  `READY TO MERGE`. Don't stop at "pushed".
- When the orchestrator says your PR is next in line, rebase it yourself (see Merge below).

## Build

- **Legacy is debt (owner rule).** All pre-HAT SmartTrader code is legacy. The HAT specs are what must be built correctly. Where legacy code, legacy tests or legacy behaviour conflict with the specs, or would bend the HAT design, change or delete the legacy code, and change or delete its tests to match. Don't add shims or compromises to keep legacy behaviour. List what you removed or changed under "Legacy changes" in the PR. Don't delete legacy code that doesn't conflict just to tidy up; that belongs in a separate PR.

- Read "Engineering guidelines" in `CLAUDE.md` before writing code; they are review findings that already cost earlier PRs a round. When you hit a failure an earlier PR also hit, add a rule there (code) or here (process) in your PR.
- **Size: at most ~2,500 changed lines per PR.** Estimate before you build. If your plan section is bigger, stop and end your turn with `BLOCKED #none: split proposal` and a proposed split into PRs that each have their own checks; the orchestrator decides. (PR 12 went to 9,450 lines and took over two hours.)
- Scope = your PR's section of the plan, plus the spec sections it lists. Do not start other PRs' work. If something in the plan is wrong or blocked, choose the option most faithful to the specs, note it in the PR description under "Deviations", and keep going.
- **Verification, not unit tests (owner's rule; overrides the plan and docs T16).** Do not write example-based unit tests for HAT code. Prove the spec's properties instead, following https://raw.githubusercontent.com/cursor/plugins/main/pstack/skills/create-formal-verification-skill/SKILL.md and the verification section of `docs/specs/hat/`:
  - Ordering, concurrency and state-machine promises: a TLA+ model in `verification/tla/`, checked with TLC (pinned version). Every action in the model points to the code it represents; state bounds and assumptions in the model header.
  - Every-input promises on Python code (formulas, caps, bounds, scoring, adjustments): Hypothesis property tests on the real code under `backend/tests/hat/properties/`.
  - Invariants the database enforces: a `@pytest.mark.postgres` check.
  - Each property must be shown failing on a deliberately broken variant (removed check, reordered step) before you trust it; record that in the PR description.
  - If the current code breaks a property, it's a finding: report the counterexample in the code's words, never weaken the property to pass.
  - For each critical area your PR builds, add or update `.claude/skills/verify-<area>/SKILL.md` (frontmatter, files/functions covered, one run command, how to tell a real bug from a modeling error, sync commit).
  - Existing repo unit tests stay unless they test legacy behaviour that conflicts with the specs; then change or delete them and say so. Reuse `FakeBroker`, `freeze_market`, `FixedClock` where a property test needs them. Never call real Fyers, NSE or Telegram; fake HTTP with recorded fixtures.
- Java 17+ for TLC and the TLC jar version pinned in `verification/`.
- Zero new ruff errors and zero new warnings. Route or response-model changes need `pnpm gen:api` and the regenerated `frontend/lib/api/schema.d.ts` committed.
- **Ready means all of these on your head commit**: every CI check SUCCESS; Sonar 0 open issues and 0 hotspots; a reply to every CodeAnt inline thread **and** to every point in the CodeAnt Nitpicks and PR Risk comments (`gh api repos/abhij1306/algotrading/issues/<n>/comments`). Only then report `READY TO MERGE`.
- Before pushing, run locally: from the worktree root, `ruff format --check .` and `ruff check .`; from `backend/`, the tests your diff touches plus `tests/hat/conformance` (HARD RULE 1), `python -m pytest -m postgres --no-cov -q <your db test files>` when you touch schema, and `pnpm check` when you touch the frontend.
- Commits: conventional style like the existing history (`feat(hat): ...`, `docs(hat): ...`). End every commit message with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## PR

- `gh pr create --base main` with a description: summary, spec requirement IDs implemented, tests added, deviations, follow-ups. End the description with:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- Wait for CI without foreground `sleep`: `gh pr checks <n> --watch --interval 30`. Jobs: backend, backend-postgres, frontend, api-schema, SonarCloud Code Analysis.
- SonarCloud (public project key `abhij1306_algotrading`, no token needed):
  - gate: `curl -s "https://sonarcloud.io/api/qualitygates/project_status?projectKey=abhij1306_algotrading&pullRequest=<n>"`
  - issues: `curl -s "https://sonarcloud.io/api/issues/search?componentKeys=abhij1306_algotrading&pullRequest=<n>&resolved=false&ps=100"`
  - hotspots: `curl -s "https://sonarcloud.io/api/hotspots/search?projectKey=abhij1306_algotrading&pullRequest=<n>&ps=100"`
  Fix every new issue and hotspot in your diff (not just gate-failing ones), unless it is a clear false positive; then explain why in the PR.
- CodeAnt (`codeant-ai[bot]`) reviews the PR automatically after it opens; it usually takes 5 to 15 minutes. Read all of it: inline review comments (`gh api repos/abhij1306/algotrading/pulls/<n>/comments`), the "CodeAnt Nitpicks" and "PR Risk" issue comments (`gh api repos/abhij1306/algotrading/issues/<n>/comments`). Fix every valid point. Reply on the PR the way the owner does: `Re CodeAnt <what>: fixed in <sha>. <one line on how>`, or a short reason when you disagree.
  - **NEVER post `@codeant-ai: review` or ask CodeAnt for a re-review in any form.**
  - If CodeAnt has not posted a review 30 minutes after the PR opened, proceed without it and say so in your report.
  - To wait for it without foreground `sleep`, run a polling loop with `run_in_background` and wait for its completion notification.
- CodeRabbit is skipped on this repo; ignore it.

## Merge (orchestrator only, owner rule)

Builders do NOT merge. When your PR is ready (all CI green, Sonar gate OK, every CodeAnt and Sonar point fixed or answered), stop and report "ready to merge" with the PR number. The orchestrator re-checks and runs `gh pr merge --squash --delete-branch`, then removes the worktree. The rules below describe what "ready" means.


- Merge only when: all CI jobs pass, the Sonar quality gate passes, and every CodeAnt and Sonar point is fixed or answered.
- Rebase only when the orchestrator tells you it's your turn (HARD RULE 5): `git fetch origin && git rebase origin/main`, rerun the touched tests, `git push --force-with-lease origin <branch>`, wait for CI. You own the rebase: resolve every conflict and any CI, Sonar or CodeAnt fallout yourself, then report `READY TO MERGE #<n> <sha>` again.
- The orchestrator runs `gh pr merge <n> --squash --delete-branch --match-head-commit <sha>`. The orchestrator removes the worktree and closes your tab after the merge.

## Never

- Push to `main` directly, or force-push `main`.
- Read credential files (`.env`, `~/.config/gh`, `~/.ssh`); they are denied anyway.
- Touch another builder's branch or worktree.
- Place real orders or call real broker/NSE/Telegram endpoints.

## Report back

When your PR is ready, end your turn with one line exactly: `READY TO MERGE #<n> <head sha>`. If you are blocked, end with `BLOCKED #<n>: <reason>`. The orchestrator watches for these lines. Above that line, summarise: PR number and URL, what was built, spec IDs covered, test counts, Sonar and CodeAnt points and how each was handled, deviations, and follow-ups for later PRs. If blocked, say exactly what blocks you and what you tried.

## Verification bookkeeping (from PR 2)

- Update `MERGED_PRS` in `backend/tests/hat/.../test_conf_verification.py` and `LATER` in `test_conf_schema.py` for your PR. `KNOWN_GAPS` may only shrink; never add to it to make CI pass without saying so under Deviations.
- Skill sync records are a 40-hex commit plus a sha256 digest of the covered files; CI compares the digest. **As the last step before every push**, run `python backend/tests/hat/conformance/hat_conf_skills.py --refresh` from the worktree root. It rewrites every stale digest and prints the skills it touched; re-confirm those skills' properties against your change. `--stale` only lists them. (A stale digest was the commonest red CI run.)
- Job bodies run by `run_job` must be idempotent: a crash between body return and success recording re-runs the body.
- TLC runner: `verification/tla/run.sh`. Add broken `.cfg` variants next to each new model, as `JobScheduler` does.
- Another builder may be working in parallel. If your rebase conflicts in the shared bookkeeping files, keep both sides' entries.

## Working alongside other builders

- At most **two builders** run at once (owner rule).
- Keep Hypothesis data sizes bounded (`max_size` on lists, frames of at most a few thousand rows), and never load the full history or universe in tests.
- Scratch files (break scripts, drafts, outputs) go in your own `prNN/` folder under your session scratchpad, never in a shared folder another builder could overwrite.
- Never use `pgrep -f` to wait in any form (`while pgrep -f`, `tail --pid=$(pgrep -f ...)`). The waiting shell's own command line contains the pattern, so the loop matches itself and never ends. Use `run_in_background` on the command itself and wait for its completion notification, or `wait <pid>`.

## Speed rules (owner flagged a 3.5 h PR, 2026-10-21)

- Locally, run only what your diff touches: the HAT tests you changed or added, plus `tests/hat/conformance`. Use the default (dev) Hypothesis profile. Never run the full suite locally (HARD RULE 1); CI runs it on every push.
- Batch your break runs: one script, all breaks, one capped run. Don't re-run every break after small review fixes; re-run only the breaks for the checks you changed.
- Push review fixes as one commit, not several. Every push restarts CI, which takes about 15 minutes.
- Wait for CI with `gh pr checks <n> --watch` in the background. Don't cancel and restart runs.
