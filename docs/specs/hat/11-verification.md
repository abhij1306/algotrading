# 11: Verification

How every HAT requirement is proven. Requirement format and the ID scheme: see [README.md](README.md).

Owner instruction (OWN-FV, 20 Oct): new HAT code is not proven with example-based unit tests. Each
requirement's acceptance is a **property**: an invariant or a temporal property stated in one plain
sentence, plus the harm if it breaks. Each property names the method that checks it. This overrides
the "unit + replay tests" wording of decision T16 (see [01-decisions.md](01-decisions.md),
HAT-DEC-T16) and the "Tests" bullets of plan section 6, which later PRs read as lists of properties
to prove. The existing SmartTrader unit tests stay untouched and keep running; this section governs
new HAT code only.

## §1 Methods

| Token on a **Verification:** line | Method | Use it for | Where it lives |
| --- | --- | --- | --- |
| `TLA` `<Model>!<Property>` | TLA+ model, checked exhaustively by TLC within stated bounds | Ordering, concurrency, retries and state-machine promises | `verification/tla/<Model>.tla` + `<Model>.cfg` |
| `PBT` `<area>.<property>` | Hypothesis property-based test on the real Python code | Properties that must hold for every input (formulas, caps, scoring, parsers, adjustments) | `backend/tests/hat/properties/test_<area>.py`, function `test_<property>` |
| `DB` `<name>` | Postgres-marked check (`@pytest.mark.postgres`) | Only invariants the database itself enforces (triggers, CHECKs, unique and partial unique indexes) | `backend/tests/hat/db/` |
| `CONF` `<name>` | Conformance check, exhaustive over one static artefact | Facts with no behaviour to quantify over: schema matches the spec column lists, seeds equal the spec YAML, the import graph, the event catalogue, the CI config; also agreement with one fixed reference result (a published worked example, a committed golden replay) | `backend/tests/hat/conformance/` |
| `OPS` `<name>` | Operational check recorded in the runbook | Deployment facts code cannot see (NTP, static IP, UPS, VPN) | [10-ops.md](10-ops.md) §9 |

`CONF` and `OPS` are not part of the owner's three methods. They exist because some requirements
(a column list, a seed value, a host setting) have no input space or state space to quantify over.
A requirement uses them only when no `TLA`, `PBT` or `DB` property fits.

#### HAT-VER-010: Every requirement carries a checkable property
- **Statement:** Every `HAT-*` requirement has at least one `Property:` acceptance bullet with a `Harm:` sentence and at least one verification method from §1.
- **Source:** OWN-FV; PLAN §5.1.
- **PR:** 1
- **Verification:** CONF `spec_requirements_well_formed`
- **Acceptance:**
  - Property: for every `#### HAT-` heading in `docs/specs/hat/*.md`, the block that follows contains a **Verification:** line whose methods are all in {TLA, PBT, DB, CONF, OPS} and at least one `Property:` bullet that contains `Harm:`. Harm: a requirement without a checkable property can be "implemented" without anyone being able to show it holds.

#### HAT-VER-011: Example tests are not acceptance for new HAT code
- **Statement:** A HAT PR proves its requirements with the methods in §1; example-based unit tests may support development but never count as a requirement's verification.
- **Source:** OWN-FV.
- **PR:** 2-17
- **Verification:** CONF `traceability_points_to_methods`
- **Acceptance:**
  - Property: every row of the README traceability table that a merged PR has filled names a `TLA`, `PBT`, `DB`, `CONF` or `OPS` artefact that exists in the repo at that commit. Harm: coverage is claimed for checks that only try the cases someone thought of.

## §2 Targets ranked by harm

The ranking orders the work: a higher target gets its model or property suite first and the
strictest review. Harm means money lost or a position left unprotected, before data or reporting
errors.

| Rank | Target | Main promise | Method | Model or area | Verify skill |
| --- | --- | --- | --- | --- | --- |
| 1 | Execution router | Shadow never reaches the broker; confirm needs a valid take; confirm and auto need a broker with GTT for delivery; no order on a stale feed or outside market hours | TLA | `ExecutionRouter` | `verify-execution` |
| 2 | Order idempotency and reconciliation | One signal never yields two entry orders; an unconfirmed order is found by tag; reconcile before the first order | TLA | `OrderReconciliation` | `verify-execution` |
| 3 | Stop placement and stop-only-tightens | Every real open position always has a resting broker stop (intraday SL, then GTT); a stop never moves away from price | TLA, DB | `StopPlacement`; `hat_stop_only_tightens` | `verify-stops` |
| 4 | Kill switch and manage-only | A tripped switch blocks every new entry and never blocks an exit | TLA | `KillSwitch` | `verify-risk` |
| 5 | Sizing and caps | Size never exceeds any of the four sizing bounds or any cap | PBT | `sizing`, `caps`, `exposure` | `verify-risk` |
| 6 | Approval tokens | A token is accepted at most once, only before expiry, only for its signal, only with a valid signature | TLA, PBT | `ApprovalToken`; `telegram` | `verify-execution` |
| 7 | Trade lifecycle | Every trade follows the 11-state machine and ends in exactly one of 4 endings; state replays from events | TLA | `TradeLifecycle` | `verify-execution` |
| 8 | Event log | Events are append-only and written in the same transaction as the state change | DB, PBT | `hat_event_is_append_only` | `verify-data` |
| 9 | Scheduler | A job runs at most once to success per (job, trade_date); the daily cycle runs in order; 22:00 fallback | TLA | `JobScheduler`, `NightlyPipeline`, `DailyCycle` | `verify-scheduler` |
| 10 | Data-quality gate and corporate actions | Scans never run on data that failed the gate; adjusted prices are raw times cumulative factors | PBT | `quality_gate`, `corporate_actions`, `features` | `verify-data` |
| 11 | Portfolio layer | Allocator multipliers stay in [0.25, 1.0] and move at most 0.25 per day; regime changes only after 3 days; lifecycle guards hold | PBT, TLA | `allocator`, `health`, `regime`; `RegimeConfirmation`, `SleeveLifecycle` | `verify-portfolio` |
| 12 | Backtest | Cost model, deflated Sharpe and trial counting are correct for every input | PBT | `costs`, `dsr` | `verify-portfolio` |
| 13 | Criteria and scoring | Every criterion returns a score in [0,1] and is a pure function of its inputs | PBT | `criteria`, `scoring`, `config` | `verify-portfolio` |

#### HAT-VER-020: Verification order follows the harm ranking
- **Statement:** Each target in the §2 table is verified by the PR that implements it, and no PR that implements a rank 1-7 target merges without its TLA+ model passing in CI.
- **Source:** OWN-FV.
- **PR:** 10, 11, 13, 15, 16, 17
- **Verification:** CONF `ci_runs_models_for_merged_targets`
- **Acceptance:**
  - Property: for every model named in §2 whose covered code exists on `main`, the CI `verification` job runs that model and fails the build when TLC reports a violation. Harm: the highest-harm promises ship on reasoning alone.

## §3 TLA+ models

The eleven models. Each one is small, describes the design at the level of the code's atomic steps, and
names the code location of every action. A later PR may split a model, but may not add a model
outside this list without updating this section.

| Model | Promises it checks | Implemented by PR | Spec sections |
| --- | --- | --- | --- |
| `TradeLifecycle` | 11 states, 4 endings, legal transitions only, every pre-entry state eventually opens or ends, a fired exit eventually closes | 11, 15 | 07 §4 |
| `ExecutionRouter` | Mode routing (shadow/confirm/auto), mode changes effective next session, no order when stale, outside hours, killed, or (confirm or auto) without GTT | 15 | 07 §1-§3, 06 §6-§7 |
| `ApprovalToken` | Signed, single-use, expiring token; price re-check against max-chase at take; `/kill` cancels pending approvals | 15, 16 | 08 §4, 07 §3 |
| `StopPlacement` | Stop only tightens; intraday SL on every real fill; GTT swap around 15:25 leaves no unprotected gap; nightly GTT verify; a lost GTT answer is settled and never blocks a sale | 13, 15, 18 | 07 §5-§6, 03 §5 |
| `OrderReconciliation` | Persist PENDING before the broker call; tag idempotency; unconfirmed submit resolved by tag; reconcile before the first order; a rejected order is retried at most once | 13, 15 | 07 §10-§11 |
| `KillSwitch` | Account, bot, manual and health switches; manage-only blocks entries and never exits; manual re-arm vs automatic re-arm for health | 10 | 06 §6 |
| `JobScheduler` | At most one success per (job, trade_date); a failed job can be retried; `exits.close` only in its window; login prompts and reports at their times | 2, 15, 16, 17 | 10 §2-§3 |
| `NightlyPipeline` | The 18:00-22:00 poll and the 22:00 fallback; the nightly chain order; no scan on no data or a failed gate; exits independent of the chain; the adjustment independent of the gate and the snapshot every night (PR 33) (split out of `JobScheduler` in PR 7) | 7, 17, 33 | 10 §3, 02 §4, §6 |
| `DailyCycle` | The intraday order (login prompt, live preparation, pre-open gaps, gap scan, the 09:20 gate refresh, MA exits, GTT swap, session end, reports); entries only 09:20-15:20 after the gate refresh and with a live broker session; no `exits.close` after `gtt.swap`; `allocate` only after `regime` succeeded and never touching open positions (split out of `JobScheduler` in PR 17) | 14, 15, 17 | 10 §3, 05 §5 |
| `RegimeConfirmation` | The confirmed regime state changes only after 3 consecutive days in the new state | 9 | 05 §3 |
| `SleeveLifecycle` | research, backtested, shadow, live, probation, retired: only guarded transitions; regime never changes lifecycle state | 9, 12 | 05 §6 |

#### HAT-VER-030: Model files and toolchain are pinned
- **Statement:** Each model is `verification/tla/<Model>.tla` with a `<Model>.cfg`, checked with TLC from `tla2tools.jar` v1.7.4 (pinned by URL and sha256) on Java 17 or later, through one command `verification/tla/run.sh [<Model>]`.
- **Source:** OWN-FV.
- **PR:** 2 (toolchain and runner), then each PR in the §3 table
- **Verification:** CONF `tla_toolchain_pinned`
- **Acceptance:**
  - Property: for every `.tla` file under `verification/tla/`, a `.cfg` with the same name exists, and `run.sh` downloads exactly the pinned jar version and refuses to run when its sha256 differs. Harm: two machines check different tool versions and disagree about the same model.
- **[OWNER-PENDING]** TLC version: default v1.7.4 (latest stable release when written); owner to confirm or bump.

#### HAT-VER-031: Every model action maps to code
- **Statement:** Every action in a model carries a comment naming the module and function (`backend/app/hat/...:function`) that performs it, and each action is one atomic step in that code (one DB transaction or one broker call).
- **Source:** OWN-FV.
- **PR:** each PR in the §3 table
- **Verification:** CONF `tla_actions_mapped`
- **Acceptance:**
  - Property: for every action defined in every model, its code-location comment names a function that exists in the repo at the same commit. Harm: the model drifts from the code and proves a design nobody runs.

#### HAT-VER-032: Every property is shown to fail on a broken variant
- **Statement:** For every model property, the repo holds a broken variant (a removed guard or reordered step, as a `.cfg` constant or a `Broken<Model>.tla`) on which TLC reports a violation, and CI runs it expecting failure.
- **Source:** OWN-FV ("Able to fail").
- **PR:** each PR in the §3 table
- **Verification:** CONF `tla_broken_variants_fail`
- **Acceptance:**
  - Property: for every property named in a model's `.cfg`, at least one broken-variant run exists and the CI job fails if that run does not report a violation of that property. Harm: a vacuous property passes forever and gives false confidence.

#### HAT-VER-033: Bounds and assumptions are stated per model
- **Statement:** Every model's header states its bounds (for example number of signals, positions, days, retries) and its assumptions (for example fair scheduling of the broker, the clock is monotone), and its verify skill repeats them.
- **Source:** OWN-FV ("Honest about scope").
- **PR:** each PR in the §3 table
- **Verification:** CONF `tla_bounds_declared`
- **Acceptance:**
  - Property: every `.tla` file begins with a comment block containing `Bounds:` and `Assumptions:` lines, and every constant in its `.cfg` is listed under `Bounds:`. Harm: a pass is read as covering cases the model never explored.

#### HAT-VER-034: Findings stay findings
- **Statement:** When a model or property finds that the code breaks a requirement, the PR reports the counterexample as a sequence of steps in the code's words and fixes the code or records the finding; it never weakens the property or the model to make it pass.
- **Source:** OWN-FV ("Findings stay findings").
- **PR:** 2-17
- **Verification:** CONF `verify_skill_findings_recorded`
- **Acceptance:**
  - Property: every change to a model property or Hypothesis strategy that narrows what is checked appears in the area's verify skill under "Scope changes" with a reason. Harm: a real bug is hidden by quietly shrinking the check.

## §4 Hypothesis property suite

#### HAT-VER-040: Properties run on the real code with a fixed profile
- **Statement:** Hypothesis properties import and call the real `backend/app/hat` functions (never a re-implementation), and run under a registered `ci` profile: `derandomize=True`, `max_examples=500`, `deadline=None`, `print_blob=True`.
- **Source:** OWN-FV ("test the real code with generated inputs, not a hand model of the function").
- **PR:** 2 (profile and dependency), then every PR with PBT properties
- **Verification:** CONF `hypothesis_profile_registered`
- **Acceptance:**
  - Property: every module under `backend/tests/hat/properties/` imports at least one function from `app.hat` and defines no function that duplicates the formula it checks, and `backend/tests/hat/conftest.py` loads the `ci` profile when `CI` is set. Harm: a property checks a copy of the formula and stays green while the real code is wrong.
- **[OWNER-PENDING]** `max_examples=500`: default chosen so the suite stays under a few minutes; owner to confirm.

#### HAT-VER-041: Money and quantity properties use exact arithmetic
- **Statement:** Properties over money, quantities, risk and weights generate `Decimal` inputs (and integers for quantities) and compare exactly, never with float tolerances.
- **Source:** SCH "Summary and conventions" (money `numeric(18,4)`, never float); PLAN C2.
- **PR:** 9, 10, 12
- **Verification:** CONF `pbt_money_strategies_decimal`
- **Acceptance:**
  - Property: every Hypothesis strategy in the `sizing`, `caps`, `exposure`, `allocator`, `costs` areas that produces a money or weight value produces `Decimal`, and assertions on those values use exact comparison. Harm: rounding drift lets a size or weight cross a cap by a fraction and the check still passes.

## §5 Database checks

#### HAT-VER-050: DB checks only for DB-enforced invariants
- **Statement:** A `DB` check exists exactly for each invariant that [03-schema.md](03-schema.md) §8 says the database enforces, and runs in CI job `backend-postgres`.
- **Source:** SCH "Governance enforced in the database"; OWN-FV; PLAN §3 "Migrations" step 5.
- **PR:** 2, 3
- **Verification:** CONF `db_checks_match_governance_table`
- **Acceptance:**
  - Property: every `DB` name on a Verification line in any spec file also appears in 03-schema.md, and once the PR named on that requirement has merged, each such name has exactly one `@pytest.mark.postgres` check under `backend/tests/hat/db/`. Harm: a trigger is removed or never created and nothing notices because no check exercises it.

## §6 CI job

#### HAT-VER-060: One CI job runs TLC and the Hypothesis suite
- **Statement:** `.github/workflows/ci.yml` gains verification jobs that together install Java 17+ and the pinned TLC, run every model and every broken variant (`verification-tla`), run the conformance checks (`verification-conformance`), and run `backend/tests/hat/properties` under the `ci` Hypothesis profile, sharded across a matrix of parallel jobs (`verification-properties`, each with pytest-xdist). The shards are a partition of the property files (CONF `ci_verification_job`).
- **Source:** OWN-FV.
- **PR:** 2
- **Verification:** CONF `ci_verification_job`
- **Acceptance:**
  - Property: on every pull request, the verification jobs fail whenever any model reports a violation, any broken variant reports none, or any Hypothesis property fails. Harm: a broken guarantee merges because nothing ran the check.
  - Property: each verification job's runtime stays under 5 minutes (hard timeout 10). Harm: agents stop rerunning a slow check on every change.
- **[OWNER-PENDING]** 5-minute budget per job: default; owner to confirm. The verification jobs (`verification-tla`, `verification-conformance`, `verification-properties (1..4)`) are added to the required checks the protocol lists (backend, backend-postgres, frontend, api-schema, SonarCloud). The `backend` job runs only the legacy suite; it no longer runs `tests/hat/properties` or `tests/hat/conformance`.

## §7 Project-local verify skills

Each critical area gets a skill an agent can follow cold, mid-change, without learning TLA+.

| Skill | Covers |
| --- | --- |
| `.claude/skills/verify-execution/SKILL.md` | `ExecutionRouter`, `OrderReconciliation`, `ApprovalToken`, `TradeLifecycle`; PBT `tags`, `telegram` |
| `.claude/skills/verify-stops/SKILL.md` | `StopPlacement`; DB `governance.stop_only_rises`, `governance.rescale_moves_stop`; PBT `stops`, `exits`, `rescale` |
| `.claude/skills/verify-risk/SKILL.md` | `KillSwitch`; PBT `sizing`, `caps`, `exposure` |
| `.claude/skills/verify-scheduler/SKILL.md` | `JobScheduler`, `NightlyPipeline`, `DailyCycle`; PBT `ops`; DB `ops.job_run_unique`; CONF verification harness |
| `.claude/skills/verify-governance/SKILL.md` | DB `governance`, `reference`, `portfolio`, `migration` checks; PBT `ids`, `time`, `config`, `risk_policy`; CONF `schema`, boundaries |
| `.claude/skills/verify-data/SKILL.md` | DB event-log checks; PBT `quality_gate`, `corporate_actions`, `features`, `ingest` |
| `.claude/skills/verify-live-data/SKILL.md` | The tick hook, 5-minute bars, staleness, pre-open gap capture, the 09:16 gap scan and announcements: PBT `bars` (live), `gapscan`, `announcements`; CONF `imports.no_second_websocket`, `single_feed_import_rule` |
| `.claude/skills/verify-standalone/SKILL.md` | The decoupling from legacy code and the owned copies: PBT `metrics`; CONF `imports.hat_decoupled_from_legacy`, `hat_suites_boot_no_legacy_app`, `legacy_does_not_import_hat`, `fixtures.vcp_oracle_frozen` |
| `.claude/skills/verify-portfolio/SKILL.md` | `RegimeConfirmation`, `SleeveLifecycle`; PBT `allocator`, `health`, `regime`, `costs`, `dsr`, `criteria`, `scoring`, `config` |

#### HAT-VER-070: Each verify skill is complete and registers
- **Statement:** Each skill in the §7 table has frontmatter (`name`, `description` saying when to use it), states each property and its harm in plain words, gives the one command to run it, explains how to tell a real bug from a modelling error, and how to update the verification for an intended change.
- **Source:** OWN-FV ("Generate the verify skill").
- **PR:** the first PR that ships code in the skill's area (10, 13, 15, 2, 7, 9)
- **Verification:** CONF `verify_skills_complete`
- **Acceptance:**
  - Property: every `.claude/skills/verify-*/SKILL.md` starts with YAML frontmatter containing `name` and `description`, and contains the sections `Properties`, `Run`, `Bug or model error`, `Updating`, `Coverage`, `Sync commit`, with no placeholder text. Harm: the next agent cannot run or trust the check, so it is skipped.

#### HAT-VER-071: Coverage record and sync commit
- **Statement:** Each verify skill lists the files and functions each check covers and the commit at which the verification was last confirmed to describe them; the sync commit moves forward only after that confirmation.
- **Source:** OWN-FV ("Kept in step with the code").
- **PR:** same as HAT-VER-070
- **Verification:** CONF `verify_skill_sync_current`
- **Acceptance:**
  - Property: for every verify skill, every file in its coverage record exists, and when any covered file changed after the recorded sync commit, the PR touching it either updates the sync commit or the CI check reports the skill as stale. Harm: verification silently describes old code after a refactor.

#### HAT-VER-072: Each skill is proven to catch a break
- **Statement:** Before a verify skill lands, its author follows it once on a scratch change that breaks a covered property in the real code, shows that the check catches it, drops the scratch change and shows the check back to its starting result.
- **Source:** OWN-FV ("Prove it before you hand off").
- **PR:** same as HAT-VER-070
- **Verification:** CONF `verify_skill_break_recorded`
- **Acceptance:**
  - Property: every verify skill records, for each of its checks, the scratch break that was tried and the failing output it produced. Harm: a skill that was never run is a draft that may not detect anything.
